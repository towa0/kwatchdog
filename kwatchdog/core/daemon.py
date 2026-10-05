"""The daemon: schedules checks, applies alert rules, dispatches notifications,
hot-reloads config, serves the heartbeat endpoint and executes TUI commands.

Nothing in here may take the daemon down: every check, channel, reload and
command is wrapped and failures become data (results, events, config errors).
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import random
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import secrets
from .alerts import Action, AlertEngine, AlertState
from .config import AppConfig, ChannelSpec, ConfigError, WatcherSpec, load_config
from .models import Result, Status
from .plugin import Channel, Notification, Registry, Watcher, WatcherContext, registries
from .storage import Store

log = logging.getLogger("kwatchdog.daemon")

DAEMON_META = "daemon"
CONFIG_ERRORS = "config_errors"
CONFIG_WARNINGS = "config_warnings"


class StoreContext(WatcherContext):
    """Watcher context backed by the SQLite store (state survives restarts)."""

    def __init__(self, store: Store, project: str, name: str, shared_http=None):
        super().__init__(project, name)
        self.store = store
        self._state = store.kv_get(f"state:{self.key}", {}) or {}
        self._shared_http = shared_http

    def state_set(self, key: str, value: Any) -> None:
        self._state[key] = value
        self.store.kv_set(f"state:{self.key}", self._state)

    def history(self, metric: str, limit: int = 50) -> list[float]:
        return self.store.metric_history(self.key, metric, limit)

    def heartbeat_last(self, name: str) -> float | None:
        return self.store.heartbeat_last(name)

    def http(self):
        return self._shared_http() if self._shared_http else super().http()


class Daemon:
    def __init__(self, config_path: Path | str, *, store: Store | None = None,
                 watcher_registry: Registry[Watcher] | None = None,
                 channel_registry: Registry[Channel] | None = None,
                 engine: AlertEngine | None = None, serve_heartbeat: bool = True,
                 reload_poll: float = 1.0):
        self.config_path = Path(config_path).expanduser()
        self._wreg, self._creg = watcher_registry, channel_registry
        self.store = store
        self.engine = engine or AlertEngine()
        self.serve_heartbeat = serve_heartbeat
        self.reload_poll = reload_poll
        self.config = AppConfig()
        self.watchers: dict[str, Watcher] = {}
        self.specs: dict[str, WatcherSpec] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.run_now: dict[str, asyncio.Event] = {}
        self.channels: dict[str, Channel] = {}
        self.alert_states: dict[str, AlertState] = {}
        self._mtime: float | None = None
        self._stop = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._http = None
        self._bg: list[asyncio.Task] = []
        self.started = time.time()
        self.on_notification = None  # optional callback(Notification) (embedded TUI)

    # ------------------------------------------------------------------ setup
    def _registries(self) -> tuple[Registry[Watcher], Registry[Channel]]:
        if self._wreg is None or self._creg is None:
            plugins = self.config.settings.path("plugins_dir") if self.config else None
            self._wreg, self._creg = registries(plugins)
        return self._wreg, self._creg

    def shared_http(self):
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(headers={"User-Agent": "kwatchdog/0.1"},
                                           limits=httpx.Limits(max_connections=50))
        return self._http

    def load(self) -> AppConfig | None:
        """Load + validate config. On failure keep the current config, record the error."""
        try:
            self._mtime = self.config_path.stat().st_mtime
        except OSError:
            self._mtime = None
        wreg, creg = self._registries()
        try:
            cfg = load_config(self.config_path, wreg, creg)
        except ConfigError as e:
            log.error("config not loaded: %s", e)
            if self.store:
                self.store.kv_set(CONFIG_ERRORS, [f"{e} - still running previous config"])
            return None
        plugin_errors = [f"plugin {k}: {v}" for k, v in {**wreg.errors, **creg.errors}.items()]
        if self.store is None:
            self.store = Store(cfg.settings.path("db"))
        self.store.kv_set(CONFIG_ERRORS, plugin_errors + cfg.errors)
        self.store.kv_set(CONFIG_WARNINGS, cfg.warnings)
        for e in cfg.errors:
            log.warning("config: %s", e)
        return cfg

    def apply(self, cfg: AppConfig) -> None:
        """Diff the new config against running watchers; restart only what changed."""
        assert self.store is not None
        old_cfg = self.config
        self.config = cfg
        new = {w.key: w for w in cfg.watchers()}
        for key in list(self.specs):
            if key not in new:
                self._stop_watcher(key)
                self.specs.pop(key, None)
        for key, spec in new.items():
            old = self.specs.get(key)
            if old is not None and old.fingerprint() == spec.fingerprint() and key in self.tasks:
                self.specs[key] = spec
                continue
            self._stop_watcher(key)
            self.specs[key] = spec
            self._start_watcher(spec)
        self.store.forget_watchers(set(new))
        # channels: rebuild (cheap)
        self.channels = {}
        for name, cs in cfg.channels.items():
            if cs.config is not None and not cs.error and not cs.unavailable:
                cls = self._registries()[1].get(cs.type)
                if cls:
                    self.channels[name] = cls(name, cs.config)
        if old_cfg.path is not None:
            log.info("config reloaded: %d watchers, %d channels", len(new), len(self.channels))

    def _start_watcher(self, spec: WatcherSpec) -> None:
        assert self.store is not None
        key = spec.key
        if spec.error:
            self.store.set_status(key, Status.SLEEPING, f"config error: {spec.error}")
            return
        if spec.unavailable:
            self.store.set_status(key, Status.SLEEPING, f"disabled: {spec.unavailable}")
            return
        if not spec.enabled:
            self.store.set_status(key, Status.SLEEPING, "disabled in config")
            return
        cls = self._registries()[0].get(spec.type)
        assert cls is not None and spec.config is not None
        ctx = StoreContext(self.store, spec.project, spec.name, self.shared_http)
        self.watchers[key] = cls(spec.name, spec.config, ctx, timeout=spec.timeout)
        self.alert_states[key] = AlertState.from_dict(self.store.kv_get(f"alert:{key}"))
        self.run_now[key] = asyncio.Event()
        self.tasks[key] = asyncio.create_task(self._loop(key), name=f"watch:{key}")
        row = self.store.watcher_row(key)
        if row is None or row.message.startswith(("config error", "disabled")):
            self.store.set_status(key, Status.SLEEPING, "waiting for first check")

    def _stop_watcher(self, key: str) -> None:
        t = self.tasks.pop(key, None)
        if t:
            t.cancel()
        self.watchers.pop(key, None)
        self.run_now.pop(key, None)

    # ------------------------------------------------------------------- loops
    async def _loop(self, key: str) -> None:
        spec = self.specs[key]
        # stagger first runs so a big config doesn't stampede
        first_delay = random.uniform(0, min(3.0, spec.interval / 4))
        delay = first_delay
        while True:
            ev = self.run_now.get(key)
            if ev is None:
                return
            try:
                await asyncio.wait_for(ev.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            ev.clear()
            t0 = time.monotonic()
            row = self.store.watcher_row(key) if self.store else None
            if row and row.disabled:
                self.store.set_status(key, Status.SLEEPING, "disabled (press d to enable)")
            else:
                try:
                    await self.check_once(key)
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - check_once already guards
                    log.exception("loop error for %s", key)
            delay = max(0.0, spec.interval - (time.monotonic() - t0))

    async def run_check(self, watcher: Watcher, spec: WatcherSpec) -> Result:
        """check() with timeout + retries; exceptions become ALERT results."""
        attempts = spec.retries + 1
        result = Result.alert("no result")
        for attempt in range(attempts):
            t0 = time.perf_counter()
            try:
                result = await asyncio.wait_for(watcher.check(), timeout=spec.timeout + 1)
                if not isinstance(result, Result):
                    result = Result.alert(f"check() returned {type(result).__name__}, expected Result")
            except asyncio.TimeoutError:
                result = Result.alert(f"check timed out after {spec.timeout:g}s")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                result = Result.alert(f"check crashed: {type(e).__name__}: {e}",
                                      raw=traceback.format_exc(limit=8))
            if result.latency_ms is None and "latency_ms" in result.metrics:
                result.latency_ms = result.metrics["latency_ms"]
            result.ts = time.time()
            if not result.status.failing or attempt == attempts - 1:
                break
            log.info("%s failed (%s), retry %d/%d", spec.key, result.message, attempt + 1, spec.retries)
            await asyncio.sleep(spec.retry_delay)
        result.message = secrets.redact(result.message)
        result.raw = secrets.redact(result.raw)
        return result

    async def check_once(self, key: str) -> Result | None:
        w, spec = self.watchers.get(key), self.specs.get(key)
        if w is None or spec is None:
            return None
        result = await self.run_check(w, spec)
        await self.handle_result(spec, result)
        return result

    async def handle_result(self, spec: WatcherSpec, result: Result) -> None:
        assert self.store is not None
        key = spec.key
        self.store.record_result(key, result)
        st = self.alert_states.setdefault(key, AlertState())
        row = self.store.watcher_row(key)
        muted = bool(row and row.muted())
        actions = self.engine.process(st, result, spec.rule, result.ts, muted=muted)
        await self._apply_actions(spec, st, actions)

    async def _apply_actions(self, spec: WatcherSpec, st: AlertState, actions: list[Action]) -> None:
        assert self.store is not None
        key = spec.key
        for a in actions:
            if a.kind == "open":
                st.incident_id = self.store.open_incident(key, time.time(), a.status, a.message)
                self.store.add_event(key, "incident", a.status.value, a.message)
            elif a.kind == "change" and st.incident_id:
                self.store.update_incident(st.incident_id, a.status, a.message)
            elif a.kind == "close":
                iid = self._open_incident_id(key)
                if iid:
                    self.store.close_incident(iid, time.time())
                self.store.add_event(key, "resolved", "OK", a.message)
            elif a.kind == "flapping":
                self.store.set_flapping(key, True)
            elif a.kind == "flap_end":
                self.store.set_flapping(key, False)
                self.store.add_event(key, "stable", a.status.value, "stopped flapping")
            if a.kind == "escalation" and st.incident_id:
                self.store.update_incident(st.incident_id, a.status, st.message, escalated=True)
            if a.notify:
                await self.dispatch(spec, a)
            elif a.kind == "flapping":
                self.store.add_event(key, "flapping", a.status.value, a.message, "suppressed")
        self.store.kv_set(f"alert:{key}", st.to_dict())

    def _open_incident_id(self, key: str) -> int | None:
        assert self.store is not None
        for inc in self.store.incidents(key, limit=5):
            if inc.closed is None:
                return inc.id
        return None

    async def dispatch(self, spec: WatcherSpec, a: Action) -> list[str]:
        assert self.store is not None
        n = Notification(kind=a.kind, project=spec.project, watcher=spec.name, status=a.status.value,
                         message=secrets.redact(a.message), ts=time.time(), duration_s=a.duration_s)
        targets = self._targets(a)
        results = await asyncio.gather(*(self._send(ch, n) for ch in targets), return_exceptions=True)
        delivered = []
        for ch, res in zip(targets, results):
            delivered.append(f"{ch.name}:{'ok' if res is True else 'FAIL ' + str(res)[:80]}")
        if a.quiet and not targets:
            delivered.append("quiet-hours")
        self.store.add_event(spec.key, a.kind, a.status.value, n.message, ", ".join(delivered) or "no channels")
        if self.on_notification:
            try:
                self.on_notification(n)
            except Exception:
                log.exception("on_notification hook failed")
        return delivered

    def _targets(self, a: Action) -> list[Channel]:
        names = a.channels or list(self.channels)
        out = []
        for name in names:
            ch = self.channels.get(name)
            spec: ChannelSpec | None = self.config.channels.get(name)
            if ch is None or spec is None:
                continue
            if a.quiet and not spec.ignore_quiet:
                continue
            if a.kind not in ("recovery",) and a.status.rank < spec.min_severity.rank and a.status != Status.OK:
                continue
            out.append(ch)
        return out

    async def _send(self, ch: Channel, n: Notification) -> bool:
        try:
            await asyncio.wait_for(ch.send(n), timeout=30)
            return True
        except Exception as e:
            log.warning("channel %s failed: %s", ch.name, secrets.redact(f"{type(e).__name__}: {e}"))
            raise RuntimeError(secrets.redact(f"{type(e).__name__}: {e}")) from None

    async def _config_watch(self) -> None:
        while True:
            await asyncio.sleep(self.reload_poll)
            try:
                mtime = self.config_path.stat().st_mtime
            except OSError:
                continue
            if mtime != self._mtime:
                await asyncio.sleep(0.2)  # let editors finish writing
                self.reload()

    def reload(self) -> bool:
        try:
            cfg = self.load()
            if cfg is not None:
                self.apply(cfg)
                if self.store:
                    self.store.add_event("", "reload", "OK",
                                         f"config reloaded ({len(cfg.errors)} error(s))" if cfg.errors else "config reloaded")
                return True
        except Exception as e:  # never die on reload
            log.exception("reload failed")
            if self.store:
                self.store.kv_set(CONFIG_ERRORS, [f"reload failed: {e}"])
        return False

    async def _commands(self) -> None:
        while True:
            await asyncio.sleep(0.4)
            if not self.store:
                continue
            try:
                for kind, target, arg in self.store.take_commands():
                    await self.execute(kind, target, arg)
            except Exception:
                log.exception("command processing failed")

    def _match(self, target: str) -> list[str]:
        if target in ("", "*"):
            return list(self.specs)
        if target in self.specs:
            return [target]
        return [k for k in self.specs if k.split("/", 1)[0] == target]

    async def execute(self, kind: str, target: str, arg: str) -> None:
        assert self.store is not None
        keys = self._match(target)
        if kind == "run":
            for k in keys:
                if k in self.run_now:
                    self.run_now[k].set()
        elif kind == "mute":
            minutes = float(arg or 30)
            until = time.time() + minutes * 60 if minutes > 0 else None
            for k in keys:
                self.store.set_muted(k, until)
            self.store.add_event(target, "mute", "SLEEPING",
                                 f"muted {minutes:g} min" if until else "unmuted")
        elif kind == "disable":
            on = arg != "0"
            for k in keys:
                self.store.set_disabled(k, on)
                if on:
                    self.store.set_status(k, Status.SLEEPING, "disabled (press d to enable)")
                elif k in self.run_now:
                    self.run_now[k].set()
            self.store.add_event(target, "disable" if on else "enable", "SLEEPING" if on else "OK",
                                 "disabled" if on else "enabled")
        elif kind == "reload":
            self.reload()
        elif kind == "test":
            spec = self.specs.get(target) or next(iter(self.specs.values()), None)
            if spec:
                await self.dispatch(spec, Action("test", Status.WARN, "test notification from kwatchdog", notify=True))

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                for key, st in list(self.alert_states.items()):
                    spec = self.specs.get(key)
                    if not spec or not st.incident_open:
                        continue
                    row = self.store.watcher_row(key) if self.store else None
                    acts = self.engine.tick(st, spec.rule, time.time(), muted=bool(row and row.muted()))
                    if acts:
                        await self._apply_actions(spec, st, acts)
            except Exception:
                log.exception("ticker failed")

    async def _beat(self) -> None:
        n = 0
        while True:
            if self.store:
                self.store.kv_set(DAEMON_META, {"pid": os.getpid(), "ts": time.time(), "started": self.started,
                                                "config": str(self.config_path),
                                                "heartbeat_port": self._port()})
                if n % 1800 == 0:
                    try:
                        self.store.prune(self.config.settings.retention_days)
                    except Exception:
                        log.exception("prune failed")
            n += 1
            await asyncio.sleep(2)

    def _port(self) -> int | None:
        return self._server.sockets[0].getsockname()[1] if self._server and self._server.sockets else None

    # ------------------------------------------------------- heartbeat endpoint
    async def _serve(self) -> None:
        st = self.config.settings
        if not self.serve_heartbeat or st.heartbeat_port is None:
            return
        try:
            self._server = await asyncio.start_server(self._handle_http, st.heartbeat_host, st.heartbeat_port)
            log.info("heartbeat endpoint on http://%s:%s/ping/<name>", st.heartbeat_host, self._port())
        except OSError as e:
            log.error("heartbeat endpoint disabled: cannot bind %s:%s: %s", st.heartbeat_host, st.heartbeat_port, e)
            if self.store:
                errs = self.store.kv_get(CONFIG_ERRORS, []) or []
                self.store.kv_set(CONFIG_ERRORS, errs + [f"heartbeat endpoint: cannot bind port {st.heartbeat_port}: {e}"])

    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            parts = line.split()
            path = unquote(parts[1]) if len(parts) >= 2 else "/"
            path = path.split("?", 1)[0]
            if path.startswith("/ping/") and len(path) > 6 and self.store:
                name = path[6:].strip("/")
                self.store.beat(name)
                code, body = 200, f"OK {name}\n"
            elif path == "/health" and self.store:
                rows = self.store.watcher_rows()
                worst = Status.worst(r.status for r in rows.values())
                code, body = 200, json.dumps({"status": worst.value, "watchers": len(rows)}) + "\n"
            else:
                code, body = 404, "not found\n"
            writer.write(f"HTTP/1.1 {code} {'OK' if code == 200 else 'Not Found'}\r\nContent-Type: text/plain\r\n"
                         f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    # --------------------------------------------------------------------- run
    async def start(self) -> None:
        cfg = self.load()
        if cfg is None:
            if self.store is None:
                self.store = Store(self.config.settings.path("db"))
            cfg = AppConfig(path=self.config_path)
        self.apply(cfg)
        await self._serve()
        for coro in (self._config_watch(), self._commands(), self._ticker(), self._beat()):
            self._bg.append(asyncio.create_task(coro))
        log.info("daemon started: %d watchers", len(self.specs))

    async def stop(self) -> None:
        for k in list(self.tasks):
            self._stop_watcher(k)
        for t in self._bg:
            t.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        self._bg.clear()
        if self._server:
            self._server.close()
        if self._http:
            await self._http.aclose()
            self._http = None
        if self.store:
            meta = self.store.kv_get(DAEMON_META) or {}
            meta["ts"] = 0
            self.store.kv_set(DAEMON_META, meta)

    async def run_forever(self) -> None:
        await self.start()
        try:
            await self._stop.wait()
        finally:
            await self.stop()

    def request_stop(self) -> None:
        self._stop.set()


def setup_logging(log_file: Path | None, level: int = logging.INFO, console: bool = True) -> None:
    root = logging.getLogger("kwatchdog")
    root.setLevel(level)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    filt = secrets.RedactingFilter()
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(filt)
        root.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(filt)
        root.addHandler(sh)


def daemon_alive(store: Store, max_age: float = 10) -> dict | None:
    meta = store.kv_get(DAEMON_META)
    if meta and time.time() - float(meta.get("ts") or 0) < max_age:
        return meta
    return None
