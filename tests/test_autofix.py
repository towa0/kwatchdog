import asyncio
import json
import sys
import textwrap

import pytest

from kwatchdog.cli import main as cli_main
from kwatchdog.core.config import load_config
from kwatchdog.core.daemon import Daemon
from kwatchdog.core.models import Status
from kwatchdog.core.remediation import get_mode, set_mode
from kwatchdog.core.storage import Store

from .test_daemon import SCRIPT, SENT, regs  # noqa: F401

PY = sys.executable.replace("\\", "/")


def config(tmp_path, on_alert: str, extra_settings: str = "", script: str = "print('fixed')") -> str:
    (tmp_path / "fix.py").write_text(script)
    return textwrap.dedent(f"""
        settings: {{heartbeat_port: null{extra_settings}}}
        channels: {{rec: {{type: recorder}}}}
        alerts: {{default: {{min_failures: 1, cooldown: 0}}}}
        remediations:
          restart:
            command: ["{PY}", "{(tmp_path / 'fix.py').as_posix()}", "--static-arg"]
            timeout: 5
        projects:
          p:
            watchers:
              - {{name: a, type: fake, interval: 3600, on_alert: {on_alert}}}
    """)


@pytest.fixture
def make_daemon(tmp_path, regs):  # noqa: F811
    made = []

    async def _make(on_alert="{command: restart, cooldown: 0}", **kw):
        p = tmp_path / "c.yaml"
        p.write_text(config(tmp_path, on_alert, **kw))
        d = Daemon(p, store=Store(tmp_path / "a.db"), watcher_registry=regs[0], channel_registry=regs[1],
                   serve_heartbeat=False)
        d.first_run_delay = 3600  # tests drive checks manually
        await d.start()
        made.append(d)
        return d

    return _make


async def alert_and_settle(d, n=1):
    for _ in range(n):
        SCRIPT.setdefault("a", []).append("ALERT")
        await d.check_once("p/a")
        await d.remediator.wait_idle()


def test_config_allowlist_only(regs):  # noqa: F811
    cfg = load_config(None, *regs, text=textwrap.dedent("""
        remediations:
          ok: {command: "systemctl restart nginx"}
        projects:
          p:
            watchers:
              - {name: a, type: fake, on_alert: {command: "rm -rf /"}}
              - {name: b, type: fake, on_alert: {command: ok, max_runs_per_hour: 0}}
              - {name: c, type: fake, on_alert: {command: ok}}
    """))
    errs = "\n".join(cfg.errors)
    assert "'rm -rf /' is not an entry under remediations" in errs
    assert "p/b: on_alert: max_runs_per_hour" in errs
    assert cfg.remediations["ok"].command == ["systemctl", "restart", "nginx"]
    assert cfg.watcher("p/c").on_alert.command == "ok"


async def test_successful_fix_logged_and_rechecks(make_daemon):
    d = await make_daemon()
    try:
        SCRIPT["a"] = ["ALERT"]
        await d.check_once("p/a")
        await d.remediator.wait_idle()
        runs = d.store.runs("p/a")
        assert len(runs) == 1 and runs[0]["mode"] == "run" and runs[0]["exit_code"] == 0
        assert "fixed" in runs[0]["output"] and runs[0]["duration_ms"] > 0
        for _ in range(100):  # immediate re-check: the watcher loop wakes and records OK
            if d.store.watcher_row("p/a").status == Status.OK:
                break
            await asyncio.sleep(0.05)
        assert d.store.watcher_row("p/a").status == Status.OK
        assert not [n for n in SENT if n.kind == "autofix"]
    finally:
        await d.stop()


async def test_watcher_output_never_reaches_the_command(make_daemon):
    d = await make_daemon(script="import sys, json; print(json.dumps(sys.argv[1:]))")
    try:
        d.watchers["p/a"].config.note = "$(touch pwned); rm -rf / `id` && echo"
        SCRIPT["a"] = ["ALERT"]
        await d.check_once("p/a")
        await d.remediator.wait_idle()
        out = d.store.runs("p/a")[0]["output"].strip()
        assert json.loads(out) == ["--static-arg"]
    finally:
        await d.stop()


async def test_failed_fix_alerts(make_daemon):
    d = await make_daemon(script="import sys; print('nope: disk full'); sys.exit(3)")
    try:
        SCRIPT["a"] = ["ALERT"]
        await d.check_once("p/a")
        await d.remediator.wait_idle()
        fix = [n for n in SENT if n.kind == "autofix"]
        assert len(fix) == 1 and fix[0].status == "ALERT"
        assert "FAILED (exit 3): nope: disk full" in fix[0].message
        assert d.store.runs("p/a")[0]["exit_code"] == 3
    finally:
        await d.stop()


async def test_timeout_alerts(make_daemon):
    d = await make_daemon(script="import time; time.sleep(30)")
    try:
        d.config.remediations["restart"].timeout = 0.5
        SCRIPT["a"] = ["ALERT"]
        await d.check_once("p/a")
        await d.remediator.wait_idle()
        assert "FAILED (timed out)" in [n for n in SENT if n.kind == "autofix"][0].message
    finally:
        await d.stop()


async def test_rate_limit_and_cooldown(make_daemon):
    d = await make_daemon("{command: restart, cooldown: 0, max_runs_per_hour: 2}",
                          script="import sys; sys.exit(0)")
    try:
        await alert_and_settle(d, 4)
        modes = [r["mode"] for r in reversed(d.store.runs("p/a"))]
        assert modes == ["run", "run", "rate-limited"]  # 4th attempt: limited, no second notice
        limited = [n for n in SENT if n.kind == "autofix"]
        assert len(limited) == 1 and "rate limit" in limited[0].message and limited[0].status == "ALERT"
    finally:
        await d.stop()


async def test_cooldown(make_daemon):
    d = await make_daemon("{command: restart, cooldown: 10m}")
    try:
        await alert_and_settle(d, 3)
        assert [r["mode"] for r in d.store.runs("p/a")] == ["run"]
    finally:
        await d.stop()


async def test_dry_run_action_and_global_mode(make_daemon, tmp_path):
    marker = (tmp_path / "ran").as_posix()
    d = await make_daemon("{command: restart, cooldown: 0, dry_run: true}",
                          script=f"open('{marker}', 'w').write('x')")
    try:
        await alert_and_settle(d)
        assert d.store.runs("p/a")[0]["mode"] == "dry-run"
        assert not (tmp_path / "ran").exists()
        # global dry-run overrides an action that would run
        d.specs["p/a"].on_alert.dry_run = False
        set_mode(d.store, "dry-run")
        await alert_and_settle(d)
        assert [r["mode"] for r in d.store.runs("p/a")] == ["dry-run", "dry-run"]
        assert not (tmp_path / "ran").exists()
    finally:
        await d.stop()


async def test_kill_switch_off(make_daemon, tmp_path):
    marker = (tmp_path / "ran").as_posix()
    d = await make_daemon(script=f"open('{marker}', 'w').write('x')")
    try:
        set_mode(d.store, "off")
        await alert_and_settle(d, 3)
        assert [r["mode"] for r in d.store.runs("p/a")] == ["off"]  # logged once per incident
        assert not (tmp_path / "ran").exists()
    finally:
        await d.stop()


async def test_config_master_switch(make_daemon, tmp_path):
    d = await make_daemon(extra_settings=", autofix: false")
    try:
        await alert_and_settle(d)
        assert d.store.runs("p/a")[0]["mode"] == "off"
    finally:
        await d.stop()


async def test_muted_never_fixes(make_daemon):
    d = await make_daemon()
    try:
        d.store.set_muted("p/a", 9e12)
        await alert_and_settle(d)
        assert d.store.runs("p/a") == []
    finally:
        await d.stop()


async def test_require_confirm(make_daemon):
    d = await make_daemon("{command: restart, cooldown: 0, require_confirm: true}")
    try:
        await alert_and_settle(d, 2)
        runs = d.store.runs("p/a")
        assert [r["mode"] for r in runs] == ["pending"]  # no duplicate while waiting
        note = [n for n in SENT if n.kind == "autofix"][0]
        assert note.status == "WARN" and f"kwatchdog autofix confirm {runs[0]['id']}" in note.message
        set_mode(d.store, "off")
        assert "autofix is off" in await d.remediator.confirm(runs[0]["id"])
        set_mode(d.store, "on")
        await d.execute("fix_confirm", "p/a", str(runs[0]["id"]))
        await d.remediator.wait_idle()
        r = d.store.run(runs[0]["id"])
        assert r["mode"] == "run" and r["exit_code"] == 0
        assert "not pending" in await d.remediator.confirm(runs[0]["id"])
    finally:
        await d.stop()


async def test_reject(make_daemon):
    d = await make_daemon("{command: restart, cooldown: 0, require_confirm: true}")
    try:
        await alert_and_settle(d)
        rid = d.store.runs("p/a")[0]["id"]
        await d.execute("fix_reject", "p/a", str(rid))
        assert d.store.run(rid)["mode"] == "rejected"
    finally:
        await d.stop()


def test_cli_kill_switch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WATCHDOG_HOME", str(tmp_path))
    cli_main(["init"])
    assert cli_main(["autofix", "off"]) == 0
    assert get_mode(Store(tmp_path / "watchdog.db")) == "off"
    assert cli_main(["autofix", "status"]) == 0
    assert "autofix: off" in capsys.readouterr().out
    assert cli_main(["autofix", "confirm", "99"]) == 1


async def test_kill_switch_wins_over_cooldown(make_daemon):
    d = await make_daemon("{command: restart, cooldown: 10m}")
    try:
        await alert_and_settle(d)  # runs once, cooldown now active
        # the successful fix triggers an immediate re-check (default result: OK) -> incident closes
        for _ in range(100):
            if not d.alert_states["p/a"].incident_open and not d._inflight:
                break
            await asyncio.sleep(0.05)
        assert not d.alert_states["p/a"].incident_open
        SCRIPT["a"] = []
        set_mode(d.store, "off")
        await alert_and_settle(d)  # new incident inside the cooldown: still logged as 'off'
        assert [r["mode"] for r in d.store.runs("p/a")] == ["off", "run"]
    finally:
        await d.stop()
