import datetime as dt
import textwrap

import pytest

from kwatchdog.cli import main as cli_main
from kwatchdog.core.config import load_config
from kwatchdog.core.daemon import Daemon
from kwatchdog.core.digest import build_digest, evaluate_budget, month_bounds
from kwatchdog.core.models import Result, Status
from kwatchdog.core.storage import Store

from .test_daemon import SENT, regs  # noqa: F401

# a fixed "now": 2026-06-16 07:31 local; June has 30 days, so the month is half over
NOW = dt.datetime(2026, 6, 16, 7, 31).timestamp()
LT = dt.datetime.fromtimestamp


def fill(store, key, start, end, step, status_at):
    t = start
    while t < end:
        store.record_result(key, Result(status_at(t), "x", ts=t))
        t += step


def test_month_bounds():
    s, e = month_bounds(NOW)
    assert LT(s) == dt.datetime(2026, 6, 1) and LT(e) == dt.datetime(2026, 7, 1)
    s, e = month_bounds(dt.datetime(2026, 12, 31, 23).timestamp())
    assert LT(e) == dt.datetime(2027, 1, 1)


def test_budget_on_track_will_miss_exhausted(tmp_path):
    store = Store(tmp_path / "b.db")
    start, _ = month_bounds(NOW)
    # all OK for the month so far
    fill(store, "a/ok", start, NOW, 600, lambda t: Status.OK)
    b = evaluate_budget(store, "a/ok", 99.9, NOW)
    assert b.state == "ok" and b.used_s == 0 and b.projected_uptime == 100.0
    assert abs(b.budget_s - 0.001 * 30 * 86400) < 1  # 43.2 minutes

    # healthy until the last 24h, then 2% of checks failing: on budget so far, but the burn rate
    # (20x the allowed 0.1%) projects a miss
    fill(store, "a/burn", start, NOW, 600,
         lambda t: Status.ALERT if t > NOW - 86400 and int(t / 600) % 50 == 0 else Status.OK)
    b = evaluate_budget(store, "a/burn", 99.9, NOW)
    assert b.state == "will-miss", b.describe()
    assert b.burn_rate > 10 and b.projected_uptime < 99.9 and b.used_pct < 100
    assert "WILL MISS" in b.describe()

    # 1% down the whole month: already over budget
    fill(store, "a/bad", start, NOW, 600, lambda t: Status.ALERT if int(t / 600) % 100 == 0 else Status.OK)
    b = evaluate_budget(store, "a/bad", 99.9, NOW)
    assert b.state == "exhausted" and b.used_pct > 100

    # same failures with a loose target are fine
    assert evaluate_budget(store, "a/bad", 95.0, NOW).state == "ok"
    assert evaluate_budget(store, "a/none", 99.9, NOW).state == "no-data"


def test_new_watcher_needs_an_hour_and_counts_only_observed_time(tmp_path):
    store = Store(tmp_path / "b.db")
    fill(store, "a/new", NOW - 300, NOW, 30, lambda t: Status.ALERT if int(t / 30) % 2 else Status.OK)
    assert evaluate_budget(store, "a/new", 99.9, NOW).state == "no-data"  # 5 minutes of data
    # watched for 2h, 1 of 120 checks failed: ~1 minute down, not "0.8% of half a month"
    fill(store, "a/two", NOW - 7200, NOW, 60, lambda t: Status.ALERT if t == NOW - 7200 else Status.OK)
    b = evaluate_budget(store, "a/two", 99.9, NOW)
    assert b.state != "exhausted" and b.used_s < 120


def test_blocked_and_sleeping_do_not_burn_budget(tmp_path):
    store = Store(tmp_path / "b.db")
    start, _ = month_bounds(NOW)
    fill(store, "a/x", start, NOW, 600, lambda t: Status.BLOCKED if int(t / 600) % 2 else Status.OK)
    assert evaluate_budget(store, "a/x", 99.99, NOW).state == "ok"


CFG = """
    settings: {heartbeat_port: null}
    status_page: {enabled: false}
    channels:
      rec: {type: recorder}
      beep: {type: bell}
    alerts: {default: {min_failures: 1, cooldown: 0}}
    digest: {at: "07:30"}
    projects:
      web:
        slo: 99.9
        watchers:
          - {name: api, type: fake, interval: 60}
          - {name: site, type: fake, interval: 60, slo: 95}
      jobs:
        watchers:
          - {name: stale, type: fake, interval: 60}
          - {name: muted, type: fake, interval: 60}
          - {name: broken, type: nope}
"""


def test_slo_inheritance_and_validation(regs):  # noqa: F811
    cfg = load_config(None, *regs, text=textwrap.dedent(CFG))
    assert cfg.watcher("web/api").slo == 99.9 and cfg.watcher("web/site").slo == 95
    assert cfg.watcher("jobs/stale").slo is None
    bad = load_config(None, *regs, text="digest: {at: '25:00', channels: [nope]}\nprojects: {p: {slo: 150}}\n")
    errs = "\n".join(bad.errors)
    assert "digest: at" in errs and "projects.p.slo" in errs


def test_build_digest_content(tmp_path, regs):  # noqa: F811
    cfg = load_config(None, *regs, text=textwrap.dedent(CFG))
    store = Store(tmp_path / "d.db")
    since = NOW - 9 * 3600
    store.record_result("web/api", Result(Status.OK, "up", ts=NOW - 30))
    store.record_result("web/site", Result(Status.OK, "up", ts=NOW - 30))
    store.record_result("jobs/stale", Result(Status.OK, "ok", ts=NOW - 5 * 3600))  # 5h, interval 60s
    store.record_result("jobs/muted", Result(Status.OK, "ok", ts=NOW - 10))
    store.set_muted("jobs/muted", NOW + 3 * 86400)
    i = store.open_incident("web/api", NOW - 4 * 3600, Status.ALERT, "connect timeout")
    store.close_incident(i, NOW - 4 * 3600 + 1680)
    store.open_incident("web/site", NOW - 20 * 3600, Status.WARN, "too old for this digest")
    store.set_flapping("web/site", True)
    store.add_run("web/api", "restart", "x", "run")
    overall, text = build_digest(cfg, store, NOW, since, cfg.digest, daemon_started=NOW - 86400)
    assert "INCIDENTS last 9h00m (1)" in text and "web/api" in text and "(28m)" in text
    assert "too old" not in text
    assert "UPTIME 24h: web 100.00%" in text
    assert "FLAPPING: web/site" in text
    assert "jobs/stale: last check 5h00m ago (interval 1m00s)" in text
    assert "jobs/muted: muted for 3d0h more" in text
    assert "jobs/broken: config error" in text
    assert "AUTOFIX (on): 1 attempt(s)" in text
    small = cfg.digest.model_copy(update={"max_lines": 10})
    _, short = build_digest(cfg, store, NOW, since, small)
    assert len(short.splitlines()) == 10 and "more line(s)" in short


@pytest.fixture
async def daemon(tmp_path, regs):  # noqa: F811
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(CFG))
    d = Daemon(p, store=Store(tmp_path / "x.db"), watcher_registry=regs[0], channel_registry=regs[1],
               serve_heartbeat=False)
    d.first_run_delay = 3600
    clock = {"t": NOW}
    d.digester.localtime = lambda ts: LT(ts)
    await d.start()
    yield d, clock
    await d.stop()


async def test_digest_schedule_once_per_day(daemon):
    d, _ = daemon
    early = dt.datetime(2026, 6, 16, 7, 29).timestamp()
    assert not d.digester.due(early)
    await d.digester.tick(early)
    assert not [n for n in SENT if n.kind == "digest"]
    await d.digester.tick(NOW)
    digests = [n for n in SENT if n.kind == "digest"]
    assert len(digests) == 1 and digests[0].title == "[DIGEST] kwatchdog/daily"
    assert "kwatchdog digest" in digests[0].message
    await d.digester.tick(NOW + 600)
    assert len([n for n in SENT if n.kind == "digest"]) == 1  # once per day
    ev = [e for e in d.store.events() if e.kind == "digest"][0]
    assert "rec:ok" in ev.delivered and "beep" not in ev.delivered  # bell excluded by default
    await d.digester.tick(NOW + 86400)
    assert len([n for n in SENT if n.kind == "digest"]) == 2


async def test_budget_alert_once_per_day_and_on_worsening(daemon):
    d, _ = daemon
    start, _ = month_bounds(NOW)
    fill(d.store, "web/api", start, NOW, 600,
         lambda t: Status.ALERT if t > NOW - 86400 and int(t / 600) % 50 == 0 else Status.OK)
    await d.digester.check_budgets(NOW)
    alerts = [n for n in SENT if n.kind == "budget"]
    assert len(alerts) == 1 and alerts[0].status == "ALERT" and "WILL MISS" in alerts[0].message
    await d.digester.check_budgets(NOW + 3600)
    assert len([n for n in SENT if n.kind == "budget"]) == 1  # same state, same day: quiet
    fill(d.store, "web/api", NOW - 3 * 86400, NOW, 60, lambda t: Status.ALERT)  # now it's exhausted
    await d.digester.check_budgets(NOW + 7200)
    alerts = [n for n in SENT if n.kind == "budget"]
    assert len(alerts) == 2 and "EXHAUSTED" in alerts[1].message
    assert d.store.kv_get("budget:web/api")["state"] == "exhausted"


def test_cli_digest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WATCHDOG_HOME", str(tmp_path))
    cli_main(["init"])
    assert cli_main(["digest", "--hours", "12"]) == 0
    out = capsys.readouterr().out
    assert "kwatchdog digest" in out and "INCIDENTS last 12h00m: none" in out
