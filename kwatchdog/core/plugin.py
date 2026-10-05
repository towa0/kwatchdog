"""Plugin system: Watcher / Channel base classes, auto-discovery, external plugins.

Built-ins live in ``kwatchdog.watchers`` and ``kwatchdog.channels``; external
plugins are any ``*.py`` file in the plugins dir (default ``~/.watchdog/plugins``).
A broken plugin file is recorded as an error, never fatal.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import pkgutil
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from .models import Result

log = logging.getLogger("kwatchdog.plugin")


class PluginConfig(BaseModel):
    """Base for plugin option schemas. Unknown keys are errors (catches typos)."""

    model_config = ConfigDict(extra="forbid")


WatcherConfig = PluginConfig  # public alias for plugin authors
ChannelConfig = PluginConfig


class _Pluggable:
    RESERVED: ClassVar[frozenset[str]] = frozenset()  # keys the config layer owns
    type: ClassVar[str] = ""
    description: ClassVar[str] = ""
    Config: ClassVar[type[PluginConfig]] = PluginConfig
    requires: ClassVar[tuple[str, ...]] = ()  # optional modules, e.g. ("psutil",)
    platforms: ClassVar[tuple[str, ...]] = ()  # e.g. ("linux",); empty = everywhere
    source: ClassVar[str] = "builtin"

    @classmethod
    def unavailable_reason(cls) -> str | None:
        if cls.platforms and not any(sys.platform.startswith(p) for p in cls.platforms):
            return f"'{cls.type}' only works on {', '.join(cls.platforms)} (this is {sys.platform})"
        missing = [m for m in cls.requires if not _module_available(m)]
        if missing:
            pkgs = " ".join(missing)
            return f"missing optional dependency: {pkgs} (pip install {pkgs})"
        return None


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class WatcherContext:
    """What a watcher may use besides its own config. The daemon supplies a
    store-backed one; tests use the in-memory default."""

    def __init__(self, project: str = "test", name: str = "test"):
        self.project, self.name = project, name
        self._state: dict[str, Any] = {}
        self._history: dict[str, list[float]] = {}
        self._heartbeats: dict[str, float] = {}
        self._http = None

    @property
    def key(self) -> str:
        return f"{self.project}/{self.name}"

    def now(self) -> float:
        return time.time()

    # persisted per-watcher state (log offsets, baselines, ...)
    def state_get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def state_set(self, key: str, value: Any) -> None:
        self._state[key] = value

    # metric history (oldest -> newest) from previous OK/WARN/ALERT results
    def history(self, metric: str, limit: int = 50) -> list[float]:
        return self._history.get(metric, [])[-limit:]

    def heartbeat_last(self, name: str) -> float | None:
        return self._heartbeats.get(name)

    def http(self):
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(headers={"User-Agent": "kwatchdog/0.1"})
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


class Watcher(_Pluggable):
    """Subclass this. Set ``type``, a ``Config`` model, implement ``check``."""

    RESERVED = frozenset({"name", "type", "interval", "timeout", "retries", "retry_delay", "enabled",
                          "alerts", "description", "tags", "depends_on", "on_alert", "slo"})

    default_interval: ClassVar[float] = 60.0

    def __init__(self, name: str, config: PluginConfig, ctx: WatcherContext | None = None,
                 timeout: float = 10.0):
        self.name = name
        self.config = config
        self.ctx = ctx or WatcherContext(name=name)
        self.timeout = timeout  # seconds; the daemon also enforces it around check()

    async def check(self) -> Result:  # pragma: no cover - abstract
        raise NotImplementedError


class Notification(BaseModel):
    kind: str  # "alert" | "update" | "escalation" | "recovery" | "flapping" | "test"
    project: str
    watcher: str
    status: str
    message: str
    ts: float
    duration_s: float | None = None

    @property
    def title(self) -> str:
        tag = {"recovery": "RECOVERED", "escalation": "ESCALATED", "flapping": "FLAPPING",
               "autofix": "AUTOFIX", "digest": "DIGEST", "budget": "BUDGET"}.get(
            self.kind, self.status
        )
        return f"[{tag}] {self.project}/{self.watcher}"

    @property
    def text(self) -> str:
        extra = ""
        if self.duration_s:
            from .models import fmt_age

            extra = f" (after {fmt_age(self.duration_s)})"
        return f"{self.title}: {self.message}{extra}"


class Channel(_Pluggable):
    RESERVED = frozenset({"type", "ignore_quiet", "min_severity"})

    def __init__(self, name: str, config: PluginConfig):
        self.name = name
        self.config = config

    async def send(self, n: Notification) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


T = TypeVar("T", bound=_Pluggable)


class Registry(Generic[T]):
    def __init__(self, base: type[T]):
        self.base = base
        self.items: dict[str, type[T]] = {}
        self.errors: dict[str, str] = {}  # source -> error message

    def register(self, cls: type[T], source: str = "builtin") -> None:
        if not cls.type:
            raise ValueError(f"{cls.__name__} has no 'type'")
        clash = cls.RESERVED & set(cls.Config.model_fields)
        if clash:
            raise ValueError(f"{cls.__name__}.Config uses reserved option name(s): {', '.join(sorted(clash))}")
        if cls.type in self.items and self.items[cls.type] is not cls:
            log.warning("plugin type %r from %s overrides %s", cls.type, source, self.items[cls.type].source)
        cls.source = source
        self.items[cls.type] = cls

    def scan_module(self, mod: ModuleType, source: str) -> int:
        n = 0
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, self.base)
                and obj is not self.base
                and obj.__module__ == mod.__name__
                and getattr(obj, "type", "")
            ):
                self.register(obj, source)
                n += 1
        return n

    def discover_package(self, package: str) -> None:
        pkg = importlib.import_module(package)
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name.startswith("_"):
                continue
            name = f"{package}.{info.name}"
            try:
                self.scan_module(importlib.import_module(name), "builtin")
            except Exception as e:  # never crash on a broken module
                self.errors[name] = f"{type(e).__name__}: {e}"
                log.exception("failed to import %s", name)

    def get(self, type_: str) -> type[T] | None:
        return self.items.get(type_)

    def available(self) -> dict[str, type[T]]:
        return {k: v for k, v in self.items.items() if v.unavailable_reason() is None}


def load_external_plugins(directory: Path, registries: list[Registry]) -> dict[str, str]:
    """Import every ``*.py`` in ``directory`` and scan it into each registry.
    Returns {file: error} for files that failed."""
    errors: dict[str, str] = {}
    if not directory or not directory.is_dir():
        return errors
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        modname = f"kwatchdog_ext_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(modname, path)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            found = sum(r.scan_module(mod, str(path)) for r in registries)
            if not found:
                errors[str(path)] = "no Watcher/Channel subclass with a 'type' found"
        except Exception as e:
            sys.modules.pop(modname, None)
            errors[str(path)] = f"{type(e).__name__}: {e}"
            log.warning("plugin %s failed to load: %s", path, e)
    for r in registries:
        r.errors.update(errors)
    return errors


_watchers: Registry[Watcher] | None = None
_channels: Registry[Channel] | None = None


def registries(plugins_dir: Path | None = None, *, reload: bool = False) -> tuple[Registry[Watcher], Registry[Channel]]:
    """Process-wide registries (built-ins + external plugins), built once."""
    global _watchers, _channels
    if _watchers is None or reload:
        _watchers, _channels = Registry(Watcher), Registry(Channel)
        _watchers.discover_package("kwatchdog.watchers")
        _channels.discover_package("kwatchdog.channels")
        if plugins_dir is not None:
            load_external_plugins(plugins_dir, [_watchers, _channels])
    assert _channels is not None
    return _watchers, _channels
