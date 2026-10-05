"""Host watchers: process/service alive, systemd unit, disk space, CPU/RAM/temperature."""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from pydantic import Field, model_validator

from ..core.models import Result, Status
from ..core.plugin import Watcher, WatcherConfig
from ._common import Thresholds, evaluate


class ProcessConfig(WatcherConfig):
    process: str | None = None  # process name, case-insensitive ("python", "nginx.exe")
    cmdline: str | None = None  # substring that must appear in the command line
    pid_file: str | None = None
    service: str | None = None  # Windows service name
    min_count: int = Field(1, ge=1)

    @model_validator(mode="after")
    def _one(self):
        if not (self.process or self.cmdline or self.pid_file or self.service):
            raise ValueError("set one of: process, cmdline, pid_file, service")
        return self


class ProcessWatcher(Watcher):
    type = "process"
    description = "process alive by name / cmdline / PID file, or Windows service running"
    Config = ProcessConfig
    requires = ("psutil",)
    default_interval = 30

    async def check(self) -> Result:
        return await asyncio.to_thread(self._check)

    def _check(self) -> Result:
        import psutil

        c: ProcessConfig = self.config
        if c.service:
            if not hasattr(psutil, "win_service_get"):
                return Result.sleeping("'service' is Windows-only; use the 'systemd' watcher on Linux")
            try:
                svc = psutil.win_service_get(c.service).as_dict()
            except psutil.NoSuchProcess:
                return Result.alert(f"service '{c.service}' not installed")
            ok = svc["status"] == "running"
            return Result(Status.OK if ok else Status.ALERT, f"service {c.service}: {svc['status']}",
                          {"running": float(ok)}, raw=str(svc))
        if c.pid_file:
            p = Path(c.pid_file).expanduser()
            try:
                pid = int(p.read_text().strip())
            except (OSError, ValueError) as e:
                return Result.alert(f"pid file {p}: {e}")
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                return Result.ok(f"pid {pid} alive ({proc.name()})", metrics={"count": 1})
            return Result.alert(f"pid {pid} from {p} is not running")
        matches = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            info = proc.info
            if c.process and (info["name"] or "").lower() not in (c.process.lower(), c.process.lower() + ".exe"):
                continue
            if c.cmdline and c.cmdline not in " ".join(info["cmdline"] or []):
                continue
            matches.append(info)
        what = c.process or c.cmdline
        raw = "\n".join(f"{m['pid']} {' '.join(m['cmdline'] or [m['name'] or ''])}" for m in matches[:50])
        if len(matches) < c.min_count:
            return Result(Status.ALERT, f"{what}: {len(matches)} running (need {c.min_count})",
                          {"count": len(matches)}, raw)
        return Result.ok(f"{what}: {len(matches)} running", metrics={"count": len(matches)}, raw=raw)


class SystemdConfig(WatcherConfig):
    unit: str
    user: bool = False  # systemctl --user


class SystemdWatcher(Watcher):
    type = "systemd"
    description = "systemd unit is active (Linux)"
    Config = SystemdConfig
    platforms = ("linux",)
    default_interval = 30

    @classmethod
    def unavailable_reason(cls):
        return super().unavailable_reason() or (None if shutil.which("systemctl") else "systemctl not found")

    async def check(self) -> Result:
        c: SystemdConfig = self.config
        args = ["systemctl"] + (["--user"] if c.user else []) + ["show", c.unit,
                "--property=ActiveState,SubState,Result,NRestarts,LoadState"]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out_b, _ = await proc.communicate()
        out = out_b.decode(errors="replace")
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return systemd_result(c.unit, props, out)


def systemd_result(unit: str, props: dict[str, str], raw: str = "") -> Result:
    if props.get("LoadState") == "not-found":
        return Result.alert(f"{unit}: unit not found", raw=raw)
    active, sub = props.get("ActiveState", "?"), props.get("SubState", "?")
    metrics = {"restarts": float(props.get("NRestarts") or 0)}
    if active == "active":
        return Result.ok(f"{unit}: {active} ({sub})", metrics=metrics, raw=raw)
    if active in ("activating", "reloading", "deactivating"):
        return Result(Status.WARN, f"{unit}: {active} ({sub})", metrics, raw)
    return Result(Status.ALERT, f"{unit}: {active} ({sub}, result={props.get('Result', '?')})", metrics, raw)


class DiskConfig(WatcherConfig):
    path: str = "/"
    warn_percent: float = 85
    alert_percent: float = 95
    min_free_gb: float | None = None


class DiskWatcher(Watcher):
    type = "disk"
    description = "disk usage percent / free space"
    Config = DiskConfig
    default_interval = 300

    async def check(self) -> Result:
        c: DiskConfig = self.config
        path = c.path
        if sys.platform.startswith("win") and path == "/":
            path = Path.cwd().anchor
        try:
            u = await asyncio.to_thread(shutil.disk_usage, Path(path).expanduser())
        except OSError as e:
            return Result.alert(f"{path}: {e}")
        pct = 100.0 * u.used / u.total if u.total else 0.0
        free_gb = u.free / 1024**3
        metrics = {"used_percent": round(pct, 1), "free_gb": round(free_gb, 2)}
        pct = round(pct, 1)
        status, msg = evaluate(pct, Thresholds(warn_above=c.warn_percent, alert_above=c.alert_percent),
                               f"{path} used", "%")
        msg += f", {free_gb:.1f} GB free"
        if c.min_free_gb is not None and free_gb < c.min_free_gb:
            status = Status.ALERT
            msg += f" (< {c.min_free_gb:g} GB)"
        return Result(status, msg, metrics)


class SystemConfig(WatcherConfig):
    cpu: Thresholds = Field(default_factory=lambda: Thresholds(warn_above=85, alert_above=97))
    ram: Thresholds = Field(default_factory=lambda: Thresholds(warn_above=85, alert_above=95))
    temp: Thresholds = Field(default_factory=lambda: Thresholds(warn_above=70, alert_above=80))
    throttle: bool = True  # Raspberry Pi: vcgencmd get_throttled


THROTTLE_BITS = {
    0: "under-voltage NOW", 1: "ARM freq capped NOW", 2: "throttled NOW", 3: "soft temp limit NOW",
    16: "under-voltage occurred", 17: "freq capping occurred", 18: "throttling occurred", 19: "soft temp limit occurred",
}


def decode_throttled(value: int) -> tuple[Status, list[str]]:
    flags = [txt for bit, txt in THROTTLE_BITS.items() if value & (1 << bit)]
    if value & 0xF:
        return Status.ALERT, flags
    if value & 0xF0000:
        return Status.WARN, flags
    return Status.OK, flags


class SystemWatcher(Watcher):
    type = "system"
    description = "CPU / RAM / temperature, Raspberry Pi throttling"
    Config = SystemConfig
    requires = ("psutil",)
    default_interval = 30

    async def check(self) -> Result:
        import psutil

        c: SystemConfig = self.config
        cpu = await asyncio.to_thread(psutil.cpu_percent, 0.5)
        ram = psutil.virtual_memory().percent
        metrics = {"cpu_percent": cpu, "ram_percent": ram}
        parts = [evaluate(cpu, c.cpu, "cpu", "%"), evaluate(ram, c.ram, "ram", "%")]
        temp = read_temperature()
        if temp is not None:
            metrics["temp_c"] = round(temp, 1)
            parts.append(evaluate(temp, c.temp, "temp", "C"))
        if c.throttle and shutil.which("vcgencmd"):
            proc = await asyncio.create_subprocess_exec("vcgencmd", "get_throttled", stdout=asyncio.subprocess.PIPE)
            out, _ = await proc.communicate()
            try:
                val = int(out.decode().strip().split("=")[1], 16)
                st, flags = decode_throttled(val)
                metrics["throttled"] = float(val)
                if flags:
                    parts.append((st, "pi: " + ", ".join(flags)))
            except (IndexError, ValueError):
                pass
        status = Status.worst(p[0] for p in parts)
        return Result(status, ", ".join(p[1] for p in parts), metrics)


def read_temperature() -> float | None:
    try:
        import psutil

        temps = getattr(psutil, "sensors_temperatures", lambda: {})() or {}
        for key in ("cpu_thermal", "coretemp", "k10temp", "cpu-thermal", "soc_thermal", "acpitz"):
            if temps.get(key):
                return max(t.current for t in temps[key])
        for entries in temps.values():
            if entries:
                return entries[0].current
    except Exception:
        pass
    p = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(p.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None
