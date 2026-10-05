import os
import time

from kwatchdog.core.models import Status

from .conftest import Ctx


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


async def test_file_freshness(make, tmp_path):
    f = tmp_path / "out.csv"
    f.write_text("a,b\n")
    w = make("file", path=str(f), max_age="10m", warn_age="5m")
    assert (await w.check()).status == Status.OK
    age(f, 400)
    assert (await w.check()).status == Status.WARN
    age(f, 700)
    r = await w.check()
    assert r.status == Status.ALERT and "stale" in r.message and r.metrics["age_s"] >= 699


async def test_file_missing_and_min_size(make, tmp_path):
    assert (await make("file", path=str(tmp_path / "nope.log")).check()).status == Status.ALERT
    f = tmp_path / "small"
    f.write_text("x")
    r = await make("file", path=str(f), min_size=100).check()
    assert r.status == Status.ALERT and "size" in r.message


async def test_dir_and_glob_newest(make, tmp_path):
    (tmp_path / "old.log").write_text("x")
    age(tmp_path / "old.log", 3600)
    (tmp_path / "new.log").write_text("x")
    r = await make("file", path=str(tmp_path), max_age="5m").check()
    assert r.status == Status.OK and "new.log" in r.message
    r = await make("file", path=str(tmp_path / "old*.log"), max_age="5m").check()
    assert r.status == Status.ALERT
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (await make("file", path=str(empty)).check()).status == Status.ALERT


async def test_logtail_new_lines_only(make, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("ERROR old problem\n")
    ctx = Ctx()
    w = make("logtail", ctx, path=str(log), warn_patterns=[r"\b429\b"], hold=0)
    r = await w.check()  # first run starts at end: old ERROR ignored
    assert r.status == Status.OK
    with log.open("a") as f:
        f.write("INFO fine\nGET /api 429 Too Many Requests\n")
    r = await w.check()
    assert r.status == Status.WARN and "429" in r.message
    with log.open("a") as f:
        f.write("Traceback (most recent call last):\n  File x\nValueError: boom\nERROR again\n")
    r = await w.check()
    assert r.status == Status.ALERT and r.metrics["matches"] == 2 and "Traceback" in r.raw
    r = await w.check()
    assert r.status == Status.OK and r.metrics["new_lines"] == 0


async def test_logtail_partial_line_and_rotation(make, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    ctx = Ctx()
    w = make("logtail", ctx, path=str(log), hold=0)
    await w.check()
    with log.open("a") as f:
        f.write("ERR")  # partial line, not yet terminated
    assert (await w.check()).status == Status.OK
    with log.open("a") as f:
        f.write("OR split\n")
    r = await w.check()
    assert r.status == Status.ALERT and "ERROR split" in r.message
    log.write_text("ERROR after truncate\n")  # truncated/rotated: smaller than offset
    assert (await w.check()).status == Status.ALERT


async def test_logtail_ignore_and_min_matches(make, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    w = make("logtail", path=str(log), ignore=["healthcheck"], min_matches=2, hold=0)
    await w.check()
    with log.open("a") as f:
        f.write("ERROR healthcheck flaky\nERROR real\n")
    r = await w.check()
    assert r.status == Status.WARN  # only 1 counted match < min_matches


async def test_logtail_from_start(make, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("ERROR from before\n")
    assert (await make("logtail", path=str(log), from_start=True).check()).status == Status.ALERT


async def test_logtail_hold_keeps_alert(make, tmp_path, monkeypatch):
    log = tmp_path / "app.log"
    log.write_text("")
    w = make("logtail", path=str(log), hold="5m")
    await w.check()
    with log.open("a") as f:
        f.write("ERROR one\n")
    assert (await w.check()).status == Status.ALERT
    r = await w.check()  # no new lines, still inside hold window
    assert r.status == Status.ALERT and "ago" in r.message
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 400)
    assert (await w.check()).status == Status.OK
