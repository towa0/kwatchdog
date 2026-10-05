import textwrap

import pytest

from kwatchdog.core.alerts import AlertEngine, AlertState
from kwatchdog.core.config import AlertRule, load_config
from kwatchdog.core.daemon import Daemon
from kwatchdog.core.models import Result, Status
from kwatchdog.core.storage import Store

from .test_daemon import SCRIPT, SENT, regs  # noqa: F401  (fixture + fake plugins)


def cfg_text(body: str) -> str:
    return textwrap.dedent("""
        settings: {heartbeat_port: null}
        channels: {rec: {type: recorder}}
        alerts: {default: {min_failures: 1, cooldown: 0}}
    """) + textwrap.dedent(body)


def test_depends_on_resolution(regs):  # noqa: F811
    cfg = load_config(None, *regs, text=cfg_text("""
        projects:
          net:
            watchers:
              - {name: router, type: fake}
              - {name: dns, type: fake, depends_on: router}
          web:
            depends_on: [net/router]
            watchers:
              - {name: api, type: fake, depends_on: [db]}
              - {name: db, type: fake}
              - {name: all, type: fake, depends_on: net}
    """))
    assert cfg.errors == []
    w = {x.key: x for x in cfg.watchers()}
    assert w["net/dns"].depends_on == ["net/router"]
    assert w["web/api"].depends_on == ["net/router", "web/db"]
    assert w["web/all"].depends_on == ["net/router", "net/dns"]
    assert cfg.dependents("net/router") == ["net/dns", "web/api", "web/db", "web/all"]


def test_depends_on_errors(regs):  # noqa: F811
    cfg = load_config(None, *regs, text=cfg_text("""
        projects:
          p:
            watchers:
              - {name: a, type: fake, depends_on: b}
              - {name: b, type: fake, depends_on: c}
              - {name: c, type: fake, depends_on: a}
              - {name: d, type: fake, depends_on: nowhere}
              - {name: e, type: fake, depends_on: a}
    """))
    errs = "\n".join(cfg.errors)
    assert "p/a: depends_on: dependency cycle" in errs and "p/c: depends_on: dependency cycle" in errs
    assert "unknown watcher or project 'nowhere'" in errs
    w = {x.key: x for x in cfg.watchers()}
    assert not w["p/a"].runnable and not w["p/d"].runnable
    assert w["p/e"].runnable  # depending on a broken watcher is fine


def test_engine_blocked_is_silent():
    eng, rule, st = AlertEngine(), AlertRule(cooldown=0, escalate_after=60), AlertState()
    assert eng.process(st, Result(Status.ALERT, "x", ts=0), rule)  # opens + alerts
    assert eng.process(st, Result(Status.BLOCKED, "blocked", ts=10), rule) == []
    assert st.blocked and st.consecutive_failures == 0
    assert eng.tick(st, rule, 1000) == []  # no escalation while blocked
    eng.process(st, Result(Status.ALERT, "x", ts=20), rule)
    assert not st.blocked


@pytest.fixture
async def dep_daemon(tmp_path, regs):  # noqa: F811
    p = tmp_path / "c.yaml"
    p.write_text(cfg_text("""
        projects:
          net:
            watchers:
              - {name: router, type: fake, interval: 3600}
          web:
            watchers:
              - {name: api, type: fake, interval: 3600, depends_on: net/router}
              - {name: site, type: fake, interval: 3600, depends_on: net/router}
              - {name: deep, type: fake, interval: 3600, depends_on: api}
    """))
    d = Daemon(p, store=Store(tmp_path / "d.db"), watcher_registry=regs[0], channel_registry=regs[1],
               serve_heartbeat=False)
    d.first_run_delay = 3600  # tests drive checks manually
    await d.start()
    yield d
    await d.stop()


async def test_one_root_cause_alert_not_n(dep_daemon):
    d = dep_daemon
    SCRIPT.update(router=["ALERT"], api=["ALERT"], site=["ALERT"], deep=["ALERT"])
    # dependents fail first; the router hasn't been checked yet -> it is checked on demand
    for k in ("web/api", "web/site", "web/deep"):
        await d.check_once(k)
    assert [n.watcher for n in SENT] == ["router"]
    assert "root cause for 3 dependent(s): web/api, web/site, web/deep" in SENT[0].message
    rows = d.store.watcher_rows()
    assert rows["net/router"].status == Status.ALERT
    for k in ("web/api", "web/site", "web/deep"):
        assert rows[k].status == Status.BLOCKED
        assert "blocked by net/router" in rows[k].message  # transitive for deep
    assert d.store.stats("web/api", 0)["checks"] == 0  # BLOCKED doesn't burn uptime


async def test_dependent_ok_stays_ok_and_unblocks(dep_daemon):
    d = dep_daemon
    SCRIPT.update(router=["ALERT", "OK"], api=["OK", "ALERT"])
    await d.check_once("net/router")
    assert (await d.check_once("web/api")).status == Status.OK  # healthy dependent isn't blocked
    await d.check_once("net/router")  # router recovers
    r = await d.check_once("web/api")
    assert r.status == Status.ALERT  # real failure of its own now alerts normally
    assert [n.watcher for n in SENT if n.kind == "alert"] == ["router", "api"]


async def test_warn_dependency_does_not_block(dep_daemon):
    d = dep_daemon
    SCRIPT.update(router=["WARN"], api=["ALERT"])
    await d.check_once("net/router")
    assert (await d.check_once("web/api")).status == Status.ALERT


async def test_concurrent_failures_wait_for_the_root(dep_daemon):
    """Root + several dependents failing at the same moment (e.g. right after startup): the
    dependents must wait for the root's in-flight check, not race it."""
    import asyncio

    d = dep_daemon
    SCRIPT.update(router=["SLOW_ALERT"], api=["ALERT"], site=["ALERT"], deep=["ALERT"])
    results = await asyncio.gather(d.check_once("net/router"), d.check_once("web/api"),
                                   d.check_once("web/site"), d.check_once("web/deep"))
    assert [r.status for r in results] == [Status.ALERT] + [Status.BLOCKED] * 3
    assert [n.watcher for n in SENT] == ["router"]
    assert d.store.stats("net/router", 0)["checks"] == 1  # one shared check, not one per dependent
