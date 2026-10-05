"""Heartbeat (dead-man's switch) and shell command watchers."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

from pydantic import Field, field_validator

from ..core.models import Duration, Result, Status, fmt_age
from ..core.plugin import Watcher, WatcherConfig
from ._common import NO_WINDOW, Thresholds, evaluate, json_path, to_number


class HeartbeatConfig(WatcherConfig):
    ping: str  # projects call  GET http://host:8787/ping/<ping>  (or `watchdog ping <ping>`)
    max_silence: Duration
    warn_silence: Duration | None = None

    @field_validator("ping")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", v):
            raise ValueError("ping name: letters, digits, _ . - only")
        return v


class HeartbeatWatcher(Watcher):
    type = "heartbeat"
    description = "dead-man's switch: alert if a project stops pinging"
    Config = HeartbeatConfig
    default_interval = 30

    async def check(self) -> Result:
        c: HeartbeatConfig = self.config
        last = self.ctx.heartbeat_last(c.ping)
        if last is None:
            return Result.warn(f"no ping received yet on '{c.ping}'")
        age = self.ctx.now() - last
        metrics = {"silence_s": round(age, 1)}
        if age > c.max_silence:
            return Result(Status.ALERT, f"silent for {fmt_age(age)} (> {fmt_age(c.max_silence)})", metrics)
        if c.warn_silence and age > c.warn_silence:
            return Result(Status.WARN, f"silent for {fmt_age(age)}", metrics)
        return Result.ok(f"last ping {fmt_age(age)} ago", metrics=metrics)


class ShellConfig(Thresholds):
    command: str
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)  # values may use ${VAR}
    ok_exit: list[int] = Field(default_factory=lambda: [0])
    warn_exit: list[int] = Field(default_factory=list)
    nagios: bool = False  # exit 0=OK 1=WARN 2=ALERT, first stdout line = message
    regex: str | None = None  # parse a number: named group 'value' or group 1
    json_path: str | None = None  # parse stdout as JSON and take this number
    alert_regex: str | None = None  # ALERT if stdout matches
    label: str = "value"


class ShellWatcher(Watcher):
    type = "shell"
    description = "run any command: exit code + stdout parsing (regex / JSON)"
    Config = ShellConfig
    default_interval = 60

    async def check(self) -> Result:
        c: ShellConfig = self.config
        env = {**os.environ, **c.env}
        proc = await asyncio.create_subprocess_shell(
            c.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=os.path.expanduser(c.cwd) if c.cwd else None, env=env, **NO_WINDOW)
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), self.timeout)
        except asyncio.TimeoutError:
            _kill_tree(proc)
            return Result.alert(f"command timed out after {self.timeout:g}s")
        out = out_b.decode(errors="replace")
        err = err_b.decode(errors="replace")
        rc = proc.returncode
        raw = f"$ {c.command}\nexit {rc}\n--- stdout ---\n{out}" + (f"\n--- stderr ---\n{err}" if err.strip() else "")
        metrics: dict[str, float] = {"exit_code": float(rc if rc is not None else -1)}
        first = (out.strip().splitlines() or err.strip().splitlines() or [""])[0][:200]

        if c.nagios:
            status = {0: Status.OK, 1: Status.WARN, 2: Status.ALERT}.get(rc, Status.ALERT)
            return Result(status, first or f"exit {rc}", metrics, raw)

        if rc in c.ok_exit:
            status, msg = Status.OK, first or f"exit {rc}"
        elif rc in c.warn_exit:
            status, msg = Status.WARN, f"exit {rc}: {first}"
        else:
            return Result(Status.ALERT, f"exit {rc}: {(err.strip().splitlines() or [first])[-1][:200]}", metrics, raw)

        if c.alert_regex and (m := re.search(c.alert_regex, out)):
            return Result(Status.ALERT, f"output matches /{c.alert_regex}/: {m.group(0)[:80]}", metrics, raw)
        value = None
        try:
            if c.regex:
                m = re.search(c.regex, out)
                if not m:
                    return Result(Status.ALERT, f"output did not match /{c.regex}/", metrics, raw)
                gd = m.groupdict()
                value = to_number(gd["value"] if "value" in gd else (m.group(1) if m.groups() else m.group(0)))
            elif c.json_path:
                value = to_number(json_path(json.loads(out), c.json_path))
        except (ValueError, KeyError) as e:
            return Result(Status.ALERT, f"cannot parse output: {e}", metrics, raw)
        if value is not None:
            metrics["value"] = value
            vstatus, vmsg = evaluate(value, c, c.label)
            return Result(Status.worst([status, vstatus]), vmsg, metrics, raw)
        return Result(status, msg, metrics, raw)


def _kill_tree(proc) -> None:
    try:
        if sys.platform.startswith("win"):
            import subprocess

            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False,
                           **NO_WINDOW)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
