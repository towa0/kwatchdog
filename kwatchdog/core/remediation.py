"""Auto-remediation, deliberately strict.

* Only commands from the top-level ``remediations:`` allowlist can run. A
  watcher's ``on_alert.command`` is a *name*, never a command line.
* Commands run as an argv list without a shell. Nothing from check output,
  messages or metrics is ever substituted into them.
* Every attempt (run, dry-run, pending, rate-limited, rejected, off) is a row
  in ``remediation_runs`` with its exit code and output.
* Kill switch: ``kwatchdog autofix off|dry-run|on`` (stored in the DB, read on
  every attempt) and ``settings.autofix: false`` in config.
* A fix that fails, times out, or hits its rate limit sends an ALERT through
  the normal channels.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from . import secrets
from .alerts import Action, AlertState
from .models import Result, Status

if TYPE_CHECKING:
    from .config import Remediation, WatcherSpec
    from .daemon import Daemon

log = logging.getLogger("kwatchdog.autofix")

MODE_KEY = "autofix_mode"
MODES = ("on", "off", "dry-run")  # pending rows end as run | rejected | expired
ATTEMPT_MODES = ("run", "dry-run", "pending")  # count toward cooldown / rate limit


def get_mode(store) -> str:
    mode = store.kv_get(MODE_KEY, "on")
    return mode if mode in MODES else "on"


def set_mode(store, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    store.kv_set(MODE_KEY, mode)


class Remediator:
    def __init__(self, daemon: "Daemon"):
        self.d = daemon
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def store(self):
        assert self.d.store is not None
        return self.d.store

    def effective_mode(self) -> str:
        if not self.d.config.settings.autofix:
            return "off"
        return get_mode(self.store)

    # ------------------------------------------------------------- triggering
    async def after_result(self, spec: "WatcherSpec", st: AlertState, result: Result, muted: bool) -> None:
        """Called after every result. Attempts a fix while an incident is open in ALERT."""
        oa = spec.on_alert
        if oa is None or result.status != Status.ALERT or not st.incident_open:
            return
        if muted or st.flapping:
            return  # a human is on it / it fixes itself: don't touch
        key, now = spec.key, time.time()
        rem = self.d.config.remediations.get(oa.command)
        if rem is None:
            return
        mode = self.effective_mode()
        argv = " ".join(rem.command)
        if mode == "off":  # kill switch first: nothing below may run
            # one 'off' row per incident, so you can see what would have happened
            off = self.store.last_run(key, ("off",))
            if not off or (st.opened and off["ts"] < st.opened):
                self.store.add_run(key, oa.command, argv, "off")
                self.store.add_event(key, "autofix", "SLEEPING", f"autofix is OFF: did not run '{oa.command}'")
            return
        last = self.store.last_run(key, ATTEMPT_MODES)
        if last and now - last["ts"] < oa.cooldown:
            return
        pending = self.store.last_run(key, ("pending",))
        if pending:
            if now - pending["ts"] < 3600:
                return  # already waiting for a human
            self.store.set_run_mode(pending["id"], "expired")
        if self.store.count_runs(key, now - 3600, ATTEMPT_MODES) >= oa.max_runs_per_hour:
            recent_limit = self.store.last_run(key, ("rate-limited",))
            if not recent_limit or now - recent_limit["ts"] >= 3600:
                self.store.add_run(key, oa.command, argv, "rate-limited")
                await self._alert(spec, f"autofix '{oa.command}' hit its rate limit "
                                        f"({oa.max_runs_per_hour}/hour) and stopped; still {result.status.value}: "
                                        f"{result.message[:120]}")
            return
        if mode == "dry-run" or oa.dry_run:
            rid = self.store.add_run(key, oa.command, argv, "dry-run")
            self.store.finish_run(rid, None, f"dry-run: would run {rem.command!r}", 0)
            self.store.add_event(key, "autofix", "WARN", f"dry-run: would run '{oa.command}' ({argv})")
            return
        if oa.require_confirm:
            rid = self.store.add_run(key, oa.command, argv, "pending")
            msg = f"autofix '{oa.command}' needs confirmation: kwatchdog autofix confirm {rid}"
            await self.d.dispatch(spec, Action("autofix", Status.WARN, msg, notify=True))
            return
        rid = self.store.add_run(key, oa.command, argv, "run")
        self._spawn(spec, rem, rid)

    def _spawn(self, spec: "WatcherSpec", rem: "Remediation", rid: int) -> None:
        t = asyncio.create_task(self._execute(spec, rem, rid), name=f"autofix:{spec.key}")
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def confirm(self, rid: int, approve: bool = True) -> str:
        row = self.store.run(rid)
        if row is None or row["mode"] != "pending" or row.get("finished"):
            return f"run {rid} is not pending"
        spec = self.d.specs.get(row["wkey"])
        if not approve:
            self.store.set_run_mode(rid, "rejected")
            self.store.finish_run(rid, None, "rejected by user", 0)
            return f"run {rid} rejected"
        if spec is None or spec.on_alert is None:
            return f"watcher {row['wkey']} no longer has on_alert"
        mode = self.effective_mode()
        if mode != "on":
            return f"autofix is {mode}: not running {rid}"
        rem = self.d.config.remediations.get(spec.on_alert.command)
        if rem is None:
            return f"remediation '{spec.on_alert.command}' no longer exists"
        self.store.set_run_mode(rid, "run")
        self._spawn(spec, rem, rid)
        return f"run {rid} confirmed"

    # --------------------------------------------------------------- running
    async def _execute(self, spec: "WatcherSpec", rem: "Remediation", rid: int) -> None:
        lock = self._locks.setdefault(spec.key, asyncio.Lock())
        async with lock:
            code, output, ms = await run_command(rem)
            ok = code == 0
            self.store.finish_run(rid, code, output, ms)
            last = (output.strip().splitlines() or [""])[-1][:160]
            if ok:
                self.store.add_event(spec.key, "autofix", "OK",
                                     f"autofix '{spec.on_alert.command if spec.on_alert else '?'}' ok in {ms:.0f}ms")
                ev = self.d.run_now.get(spec.key)
                if ev:
                    ev.set()  # re-check right away
            else:
                what = "timed out" if code is None else f"exit {code}"
                await self._alert(spec, f"autofix '{spec.on_alert.command if spec.on_alert else '?'}' FAILED "
                                        f"({what}): {last}")

    async def _alert(self, spec: "WatcherSpec", message: str) -> None:
        await self.d.dispatch(spec, Action("autofix", Status.ALERT, message, notify=True,
                                           channels=list(spec.rule.channels)))

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


async def run_command(rem: "Remediation") -> tuple[int | None, str, float]:
    """Run an allowlisted command (argv, no shell). Returns (exit code or None on
    timeout/failure to start, combined output, duration ms)."""
    env = {**os.environ, **rem.env}
    kw = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}
    t0 = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *rem.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, cwd=os.path.expanduser(rem.cwd) if rem.cwd else None, env=env, **kw)
    except (OSError, ValueError) as e:
        return None, f"could not start {rem.command[0]!r}: {e}", (time.perf_counter() - t0) * 1000
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), rem.timeout)
    except asyncio.TimeoutError:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False, **kw)
        else:
            proc.kill()
        return None, f"timed out after {rem.timeout:g}s", (time.perf_counter() - t0) * 1000
    text = secrets.redact(out.decode(errors="replace"))
    return proc.returncode, text, (time.perf_counter() - t0) * 1000
