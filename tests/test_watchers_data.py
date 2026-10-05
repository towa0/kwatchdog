import json
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from kwatchdog.core.models import Status
from kwatchdog.watchers._common import json_path, to_number, to_timestamp
from kwatchdog.watchers.data import zscore

from .conftest import Ctx


def test_helpers():
    data = {"a": {"b": [{"c": 5}, {"c": "7.5"}]}}
    assert json_path(data, "a.b[1].c") == "7.5"
    assert json_path(data, "$.a.b.0.c") == 5
    with pytest.raises(KeyError):
        json_path(data, "a.x")
    assert to_number("1,234.5") == 1234.5
    assert to_number("3,5") == 3.5
    assert to_number("$42") == 42
    assert to_timestamp(1_700_000_000_000) == 1_700_000_000
    assert to_timestamp("2024-01-01T00:00:00Z") == 1704067200
    assert zscore(10, [10, 10, 10]) == 0
    assert zscore(20, [9, 10, 11]) > 10


def make_db(path, rows, last_ok_age=10):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS runs (ts REAL, ok INT, errors INT, requests INT)")
    con.execute("DELETE FROM items")
    con.executemany("INSERT INTO items VALUES (?, ?)", [(i, time.time() - 100) for i in range(rows)])
    con.execute("INSERT INTO runs VALUES (?, 1, 2, 100)", (time.time() - last_ok_age,))
    con.commit()
    con.close()


async def test_scraper_sqlite(make, tmp_path):
    db = tmp_path / "s.db"
    make_db(db, 100)
    q = dict(db=str(db), rows_query="SELECT COUNT(*) FROM items",
             last_success_query="SELECT MAX(ts) FROM runs WHERE ok=1",
             errors_query="SELECT SUM(errors) FROM runs", requests_query="SELECT SUM(requests) FROM runs",
             max_success_age="1h")
    r = await make("scraper", Ctx(history={"rows": [100, 98, 102]}), **q).check()
    assert r.status == Status.OK, r.message
    assert r.metrics["rows"] == 100 and r.metrics["error_rate"] == 0.02
    make_db(db, 30)
    r = await make("scraper", Ctx(history={"rows": [100, 98, 102]}), **q).check()
    assert r.status == Status.ALERT and "dropped 70%" in r.message
    make_db(db, 60)
    assert (await make("scraper", Ctx(history={"rows": [100, 100]}), **q).check()).status == Status.WARN


async def test_scraper_status_file(make, tmp_path):
    f = tmp_path / "status.json"
    f.write_text(json.dumps({"rows": 50, "last_success": time.time() - 7200, "errors": 30, "requests": 100}))
    r = await make("scraper", status_file=str(f), max_success_age="1h").check()
    assert r.status == Status.ALERT
    assert "no successful run for 2h" in r.message and "error rate 30.0%" in r.message


async def test_scraper_status_url(make):
    def h(req):
        return httpx.Response(200, json={"stats": {"n": 10}, "ok_at": "2099-01-01T00:00:00Z"})
    r = await make("scraper", Ctx(h), status_url="http://s/status", rows_key="stats.n",
                   last_success_key="ok_at").check()
    assert r.status == Status.OK and r.metrics["rows"] == 10


async def test_datafile_csv(make, tmp_path):
    f = tmp_path / "prices.csv"
    now = time.time()
    f.write_text("ts,price\n" + "".join(f"{now - 3600 + i},{i}\n" for i in range(20)))
    r = await make("datafile", path=str(f), timestamp_column="ts", max_age="30m").check()
    assert r.status == Status.ALERT and r.metrics["rows"] == 20
    r = await make("datafile", path=str(f), timestamp_column="ts", max_age="2h").check()
    assert r.status == Status.OK


async def test_datafile_sqlite_zscore(make, tmp_path):
    db = tmp_path / "d.db"
    make_db(db, 500)
    hist = {"rows": [100, 102, 98, 101, 99, 100]}
    r = await make("datafile", Ctx(history=hist), path=str(db), table="items", timestamp_column="ts").check()
    assert r.status == Status.ALERT and "anomaly" in r.message
    make_db(db, 101)
    r = await make("datafile", Ctx(history=hist), path=str(db), table="items").check()
    assert r.status == Status.OK
    assert (await make("datafile", path=str(tmp_path / "x.db"), table="t").check()).status == Status.ALERT


def test_datafile_rejects_sql_injection(make):
    with pytest.raises(Exception):
        make("datafile", path="x.db", table="t; DROP TABLE t")


async def test_json_metric(make, tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"queue": {"depth": 120}}))
    r = await make("json", file=str(f), path="queue.depth", warn_above=100, alert_above=500).check()
    assert r.status == Status.WARN and r.metrics["value"] == 120
    r = await make("json", file=str(f), path="queue.missing").check()
    assert r.status == Status.ALERT and "not found" in r.message
    r = await make("json", Ctx(lambda req: httpx.Response(200, json={"v": 1})), url="http://m/", path="v",
                   alert_below=5).check()
    assert r.status == Status.ALERT


async def test_price_thresholds_cross_and_change(make, tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"btc": {"usd": 105.0}}))
    base = dict(file=str(f), path="btc.usd", symbol="BTC")
    r = await make("price", Ctx(history={"value": [99.0]}), **base, cross_above=100).check()
    assert r.status == Status.ALERT and "crossed above 100" in r.message
    r = await make("price", Ctx(history={"value": [101.0]}), **base, cross_above=100).check()
    assert r.status == Status.OK
    r = await make("price", Ctx(history={"value": [100.0, 101, 102]}), **base, change_window=3,
                   change_warn_percent=4).check()
    assert r.status == Status.WARN and r.metrics["change_percent"] == 5.0
    r = await make("price", **base, alert_above=104).check()
    assert r.status == Status.ALERT


async def test_price_regex(make):
    html = "<span id=p>EUR 1.234,56</span>"
    r = await make("price", Ctx(lambda req: httpx.Response(200, text=html)), url="http://p/",
                   regex=r"EUR ([\d.,]+)", warn_below=2000).check()
    assert r.metrics["value"] == 1234.56 and r.status == Status.WARN


async def test_heartbeat(make):
    now = 1_000_000.0
    w = make("heartbeat", Ctx(heartbeats={"job": now - 30}, now=now), ping="job", max_silence="5m",
             warn_silence="20s")
    assert (await w.check()).status == Status.WARN
    w = make("heartbeat", Ctx(heartbeats={"job": now - 600}, now=now), ping="job", max_silence="5m")
    r = await w.check()
    assert r.status == Status.ALERT and "silent for 10m" in r.message
    assert (await make("heartbeat", ping="never", max_silence=60).check()).status == Status.WARN
    with pytest.raises(Exception):
        make("heartbeat", ping="bad name!", max_silence=60)


PY = f'"{sys.executable}"'


async def test_shell_exit_and_parse(make):
    r = await make("shell", command=f'{PY} -c "print(42)"').check()
    assert r.status == Status.OK and r.message == "42"
    r = await make("shell", command=f'{PY} -c "import sys; print(\'bad\'); sys.exit(3)"').check()
    assert r.status == Status.ALERT and "exit 3" in r.message
    r = await make("shell", command=f'{PY} -c "print(\'queue=17\')"', regex=r"queue=(?P<value>\d+)",
                   warn_above=10).check()
    assert r.status == Status.WARN and r.metrics["value"] == 17
    r = await make("shell", command=f'{PY} -c "print(\'{{\\"n\\": 3}}\')"', json_path="n", alert_below=5).check()
    assert r.status == Status.ALERT, r.raw


async def test_shell_nagios_and_alert_regex(make):
    r = await make("shell", command=f'{PY} -c "import sys; print(\'disk 91%\'); sys.exit(1)"', nagios=True).check()
    assert r.status == Status.WARN and r.message == "disk 91%"
    r = await make("shell", command=f'{PY} -c "print(\'FATAL: x\')"', alert_regex="FATAL").check()
    assert r.status == Status.ALERT


async def test_shell_timeout(make):
    t0 = time.time()
    r = await make("shell", timeout=1, command=f'{PY} -c "import time; time.sleep(20)"').check()
    assert r.status == Status.ALERT and "timed out" in r.message
    assert time.time() - t0 < 10


async def test_shell_env_and_secret_expansion(make, monkeypatch):
    monkeypatch.setenv("KW_SHELL_V", "abc")
    r = await make("shell", command=f'{PY} -c "import os; print(os.environ[\'X\'])"', env={"X": "hello"}).check()
    assert r.message == "hello"


GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]


def git(cwd, *args):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path):
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", str(remote), str(work))
    (work / "a.txt").write_text("1")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "push", "-u", "origin", "main")
    return tmp_path, remote, work


async def test_git_states(make, repos):
    tmp, remote, work = repos
    w = make("git", path=str(work), fetch=False)
    r = await w.check()
    assert r.status == Status.OK, r.message
    (work / "b.txt").write_text("dirty")
    r = await w.check()
    assert r.status == Status.WARN and "1 uncommitted" in r.message
    git(work, "add", ".")
    git(work, "commit", "-m", "two")
    r = await w.check()
    assert "1 unpushed" in r.message and r.metrics["unpushed"] == 1
    # someone else pushes -> we're behind after fetch
    other = tmp / "other"
    git(tmp, "clone", str(remote), str(other))
    (other / "c.txt").write_text("x")
    git(other, "add", ".")
    git(other, "commit", "-m", "other")
    git(other, "push")
    r = await make("git", path=str(work), fetch=True, behind="ALERT").check()
    assert r.status == Status.ALERT and "1 behind remote" in r.message


async def test_git_not_a_repo(make, tmp_path):
    assert (await make("git", path=str(tmp_path), fetch=False).check()).status == Status.ALERT


async def test_git_ci_failure(make, repos):
    _, _, work = repos

    def gh(req):
        assert req.url.path == "/repos/me/proj/actions/runs"
        return httpx.Response(200, json={"workflow_runs": [
            {"status": "in_progress", "conclusion": None, "name": "CI"},
            {"status": "completed", "conclusion": "failure", "name": "CI"}]})
    r = await make("git", Ctx(gh), path=str(work), fetch=False, check_ci=True, github_repo="me/proj").check()
    assert r.status == Status.ALERT and "CI failure" in r.message
