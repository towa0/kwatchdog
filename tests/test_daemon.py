import asyncio
import textwrap
import time

import httpx
import pytest

from kwatchdog.core import secrets
from kwatchdog.core.daemon import CONFIG_ERRORS, Daemon
from kwatchdog.core.models import Result, Status
from kwatchdog.core.plugin import Channel, ChannelConfig, Registry, Watcher, WatcherConfig
from kwatchdog.core.storage import Store

SCRIPT: dict[str, list] = {}  # watcher name -> queue of statuses to return
SENT: list = []


class FakeCfg(WatcherConfig):
    note: str = ""


class Fake(Watcher):
    type = "fake"
    Config = FakeCfg

    async def check(self):
        q = SCRIPT.setdefault(self.name, [])
        item = q.pop(0) if q else "OK"
        if item == "BOOM":
            raise RuntimeError("kaboom")
        if item == "HANG":
            await asyncio.sleep(60)
        if item.startswith("SLOW_"):
            await asyncio.sleep(0.3)
            item = item[5:]
        return Result(Status(item), f"{self.name} says {item} {self.config.note}", latency_ms=5.0)


class Recorder(Channel):
    type = "recorder"
    Config = ChannelConfig

    async def send(self, n):
        SENT.append(n)


class Broken(Channel):
    type = "broken"
    Config = ChannelConfig

    async def send(self, n):
        raise ConnectionError("down")


@pytest.fixture
def regs():
    w, c = Registry(Watcher), Registry(Channel)
    w.discover_package("kwatchdog.watchers")
    c.discover_package("kwatchdog.channels")
    w.register(Fake)
    c.register(Recorder)
    c.register(Broken)
    SCRIPT.clear()
    SENT.clear()
    return w, c


BASE = """
settings: {heartbeat_port: null}
channels:
  rec: {type: recorder}
  bad: {type: broken}
alerts:
  default: {min_failures: 1, cooldown: 0}
projects:
  p:
    watchers:
      - {name: a, type: fake, interval: 3600}
"""


@pytest.fixture
async def daemon(tmp_path, regs):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(BASE))
    store = Store(tmp_path / "w.db")
    d = Daemon(cfg, store=store, watcher_registry=regs[0], channel_registry=regs[1],
               serve_heartbeat=False, reload_poll=0.1)
    d.first_run_delay = 3600  # tests drive checks manually
    await d.start()
    yield d
    await d.stop()
    store.close()


async def wait_for(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_alert_then_recovery_dispatched(daemon):
    SCRIPT["a"] = ["ALERT", "OK"]
    await daemon.check_once("p/a")
    assert [n.kind for n in SENT] == ["alert"]
    ev = daemon.store.events()[0]
    assert ev.kind == "alert" and "rec:ok" in ev.delivered and "bad:FAIL" in ev.delivered
    inc = daemon.store.incidents("p/a")
    assert len(inc) == 1 and inc[0].closed is None
    await daemon.check_once("p/a")
    assert [n.kind for n in SENT] == ["alert", "recovery"]
    assert daemon.store.incidents("p/a")[0].closed is not None
    assert daemon.store.watcher_row("p/a").status == Status.OK


async def test_crash_and_timeout_become_alerts(daemon):
    SCRIPT["a"] = ["BOOM"]
    r = await daemon.check_once("p/a")
    assert r.status == Status.ALERT and "kaboom" in r.message and "Traceback" in r.raw
    daemon.specs["p/a"].timeout = 0.2
    SCRIPT["a"] = ["HANG"]
    r = await daemon.check_once("p/a")
    assert r.status == Status.ALERT and "timed out" in r.message


async def test_retries(daemon):
    daemon.specs["p/a"].retries = 2
    daemon.specs["p/a"].retry_delay = 0
    SCRIPT["a"] = ["ALERT", "ALERT", "OK"]
    r = await daemon.check_once("p/a")
    assert r.status == Status.OK and SENT == []


async def test_hot_reload_add_change_remove(daemon):
    cfg = daemon.config_path
    task_a = daemon.tasks["p/a"]
    text = cfg.read_text() + "      - {name: b, type: fake, interval: 3600}\n"
    cfg.write_text(text)
    await wait_for(lambda: "p/b" in daemon.tasks)
    assert daemon.tasks["p/a"] is task_a  # unchanged watcher not restarted
    cfg.write_text(text.replace("name: a, type: fake", "name: a, type: fake, note: changed"))
    await wait_for(lambda: daemon.tasks.get("p/a") is not task_a)
    cfg.write_text(textwrap.dedent(BASE).replace("name: a", "name: c"))
    await wait_for(lambda: set(daemon.tasks) == {"p/c"})
    assert set(daemon.store.watcher_rows()) == {"p/c"}


async def test_bad_reload_keeps_running(daemon):
    cfg = daemon.config_path
    cfg.write_text("projects: [this is: not valid yaml")
    await wait_for(lambda: daemon.store.kv_get(CONFIG_ERRORS))
    assert "YAML parse error" in daemon.store.kv_get(CONFIG_ERRORS)[0]
    assert "p/a" in daemon.tasks  # old config still running
    # validation error (unknown type) -> reported, other watchers keep going
    cfg.write_text(textwrap.dedent(BASE) + "      - {name: z, type: nope}\n")
    await wait_for(lambda: "p/z" in daemon.specs)
    assert any("unknown watcher type 'nope'" in e for e in daemon.store.kv_get(CONFIG_ERRORS))
    assert daemon.store.watcher_row("p/z").message.startswith("config error")
    assert "p/a" in daemon.tasks


async def test_commands_run_mute_disable(daemon):
    SCRIPT["a"] = ["ALERT"]
    daemon.store.push_command("mute", "p/a", "10")
    await wait_for(lambda: daemon.store.watcher_row("p/a").muted())
    daemon.store.push_command("run", "p")
    await wait_for(lambda: daemon.store.watcher_row("p/a").status == Status.ALERT)
    assert SENT == []  # muted
    daemon.store.push_command("mute", "p/a", "0")
    await wait_for(lambda: not daemon.store.watcher_row("p/a").muted())
    daemon.store.push_command("disable", "p/a", "1")
    await wait_for(lambda: daemon.store.watcher_row("p/a").disabled)
    daemon.store.push_command("run", "p/a")
    await wait_for(lambda: daemon.store.watcher_row("p/a").status == Status.SLEEPING)


async def test_unavailable_watcher_is_sleeping(tmp_path, regs):
    class NeedsDep(Watcher):
        type = "needsdep"
        requires = ("no_such_module_kw",)
    regs[0].register(NeedsDep)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("settings: {heartbeat_port: null}\nprojects: {p: {watchers: [{name: x, type: needsdep}]}}\n")
    d = Daemon(cfg, store=Store(tmp_path / "x.db"), watcher_registry=regs[0], channel_registry=regs[1])
    d.first_run_delay = 3600  # tests drive checks manually
    await d.start()
    try:
        row = d.store.watcher_row("p/x")
        assert row.status == Status.SLEEPING and "pip install no_such_module_kw" in row.message
        assert "p/x" not in d.tasks
    finally:
        await d.stop()


async def test_secret_redacted_in_store(daemon, monkeypatch):
    secrets.register_secret("hunter2hunter2")
    SCRIPT["a"] = ["ALERT"]
    daemon.watchers["p/a"].config.note = "token=hunter2hunter2"
    r = await daemon.check_once("p/a")
    assert "hunter2" not in r.message
    assert "hunter2" not in daemon.store.results("p/a")[0].message
    assert "hunter2" not in SENT[0].message


async def test_heartbeat_endpoint(tmp_path, regs):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(textwrap.dedent("""
        settings: {heartbeat_port: 0}
        projects:
          p:
            watchers:
              - {name: hb, type: heartbeat, ping: nightly, max_silence: 1h, interval: 3600}
    """))
    d = Daemon(cfg, store=Store(tmp_path / "h.db"), watcher_registry=regs[0], channel_registry=regs[1])
    d.first_run_delay = 3600  # tests drive checks manually
    await d.start()
    try:
        port = d._port()
        assert (await d.check_once("p/hb")).status == Status.WARN
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/ping/nightly")
            assert r.status_code == 200
            assert (await c.get(f"http://127.0.0.1:{port}/health")).json()["watchers"] == 1
            assert (await c.get(f"http://127.0.0.1:{port}/other")).status_code == 404
        assert (await d.check_once("p/hb")).status == Status.OK
    finally:
        await d.stop()


async def test_escalation_via_ticker_and_quiet_channel(tmp_path, regs):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(textwrap.dedent("""
        settings: {heartbeat_port: null}
        channels:
          rec: {type: recorder}
        alerts: {default: {min_failures: 1, cooldown: 0, escalate_after: 1}}
        projects: {p: {watchers: [{name: a, type: fake, interval: 3600}]}}
    """))
    d = Daemon(cfg, store=Store(tmp_path / "e.db"), watcher_registry=regs[0], channel_registry=regs[1])
    d.first_run_delay = 3600  # tests drive checks manually
    await d.start()
    try:
        SCRIPT["a"] = ["ALERT"]
        await d.check_once("p/a")
        st = d.alert_states["p/a"]
        acts = d.engine.tick(st, d.specs["p/a"].rule, time.time() + 5)
        await d._apply_actions(d.specs["p/a"], st, acts)
        assert [n.kind for n in SENT] == ["alert", "escalation"]
        assert d.store.incidents("p/a")[0].escalated
    finally:
        await d.stop()
