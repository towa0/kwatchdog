"""YAML config: projects -> watchers, channels, alert rules, settings.

Loading never raises for content problems: each broken watcher/channel is
recorded in ``AppConfig.errors`` (and kept as a spec carrying the error so the
TUI can show it in the tree). Only unreadable/unparseable YAML raises
``ConfigError`` - the daemon then keeps running the previous config.
"""
from __future__ import annotations

import copy
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from ruamel.yaml import YAML

from . import secrets
from .models import Duration, Status
from .plugin import Channel, PluginConfig, Registry, Watcher


class ConfigError(Exception):
    pass


def watchdog_home() -> Path:
    return Path(os.environ.get("WATCHDOG_HOME") or Path.home() / ".watchdog").expanduser()


def default_config_path() -> Path:
    return Path(os.environ.get("WATCHDOG_CONFIG") or watchdog_home() / "config.yaml").expanduser()


class AlertRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Status = Status.WARN  # minimum status that notifies (WARN or ALERT)
    min_failures: int = Field(1, ge=1)  # consecutive failing checks before an incident opens
    cooldown: Duration = 300.0  # min seconds between notifications per watcher
    flap_window: int = Field(10, ge=3)  # look at the last N statuses
    flap_threshold: float = Field(0.5, gt=0, le=1)  # fraction of state changes => flapping
    quiet_hours: str | None = None  # "23:00-07:00" local time
    escalate_after: Duration | None = None  # incident still open after this => escalate once
    channels: list[str] = Field(default_factory=list)  # empty = all channels
    escalate_channels: list[str] = Field(default_factory=list)  # empty = same as channels
    recovery: bool = True  # send recovery notices

    @field_validator("severity")
    @classmethod
    def _sev(cls, v: Status) -> Status:
        if v not in (Status.WARN, Status.ALERT):
            raise ValueError("severity must be WARN or ALERT")
        return v

    @field_validator("quiet_hours")
    @classmethod
    def _quiet(cls, v: str | None) -> str | None:
        if v:
            parse_quiet_hours(v)
        return v


def parse_quiet_hours(spec: str) -> tuple[int, int]:
    """'23:00-07:00' -> (1380, 420) minutes since midnight."""
    try:
        a, b = spec.split("-")
        def mins(s: str) -> int:
            h, m = s.strip().split(":")
            h, m = int(h), int(m)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
            return h * 60 + m
        return mins(a), mins(b)
    except Exception:
        raise ValueError(f"quiet_hours {spec!r} must look like '23:00-07:00'") from None


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # relative paths resolve against $WATCHDOG_HOME (default ~/.watchdog)
    db: str = "watchdog.db"
    plugins_dir: str = "plugins"
    heartbeat_host: str = "127.0.0.1"
    heartbeat_port: int | None = 8787  # null disables the dead-man's-switch endpoint
    log_file: str | None = "daemon.log"
    retention_days: int = 30
    default_interval: Duration = 60.0
    default_timeout: Duration = 10.0

    def path(self, attr: str) -> Path:
        p = Path(os.path.expandvars(getattr(self, attr))).expanduser()
        return p if p.is_absolute() else watchdog_home() / p


COMMON_KEYS = set(Watcher.RESERVED)


@dataclass
class WatcherSpec:
    project: str
    name: str
    type: str
    interval: float
    timeout: float
    retries: int = 0
    retry_delay: float = 2.0
    enabled: bool = True
    description: str = ""
    tags: list[str] = field(default_factory=list)
    rule: AlertRule = field(default_factory=AlertRule)
    options: dict[str, Any] = field(default_factory=dict)  # raw (unexpanded) plugin options
    config: PluginConfig | None = None  # validated, env-expanded plugin config
    error: str | None = None  # config error -> watcher can't run
    unavailable: str | None = None  # missing dep / wrong platform -> disabled
    depends_on_raw: list[str] = field(default_factory=list)  # as written (watcher, project/watcher, project)
    depends_on: list[str] = field(default_factory=list)  # resolved watcher keys

    @property
    def key(self) -> str:
        return f"{self.project}/{self.name}"

    @property
    def runnable(self) -> bool:
        return self.enabled and not self.error and not self.unavailable and self.config is not None

    def fingerprint(self) -> tuple:
        """Changes iff the watcher must be restarted on reload."""
        return (
            self.type, self.interval, self.timeout, self.retries, self.retry_delay,
            self.enabled, repr(self.options), self.rule.model_dump_json(), self.error, self.unavailable,
        )


@dataclass
class ProjectSpec:
    name: str
    description: str = ""
    enabled: bool = True
    watchers: list[WatcherSpec] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)  # applies to every watcher in the project


@dataclass
class ChannelSpec:
    name: str
    type: str
    ignore_quiet: bool = False
    min_severity: Status = Status.WARN
    config: PluginConfig | None = None
    error: str | None = None
    unavailable: str | None = None


@dataclass
class AppConfig:
    path: Path | None = None
    settings: Settings = field(default_factory=Settings)
    projects: dict[str, ProjectSpec] = field(default_factory=dict)
    channels: dict[str, ChannelSpec] = field(default_factory=dict)
    rules: dict[str, AlertRule] = field(default_factory=lambda: {"default": AlertRule()})
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def watchers(self) -> list[WatcherSpec]:
        return [w for p in self.projects.values() for w in p.watchers]

    def watcher(self, key: str) -> WatcherSpec | None:
        return next((w for w in self.watchers() if w.key == key), None)

    def dependents(self, key: str) -> list[str]:
        """Watchers that depend on ``key``, directly or transitively (BFS order)."""
        out: list[str] = []
        frontier = [key]
        while frontier:
            cur = frontier.pop(0)
            for w in self.watchers():
                if cur in w.depends_on and w.key not in out and w.key != key:
                    out.append(w.key)
                    frontier.append(w.key)
        return out


def _fmt_validation(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _yaml_load(text: str) -> Any:
    y = YAML(typ="safe", pure=True)
    return y.load(text)


def load_config(
    path: Path | str | None,
    watcher_registry: Registry[Watcher],
    channel_registry: Registry[Channel],
    *,
    text: str | None = None,
) -> AppConfig:
    path = Path(path).expanduser() if path else None
    if text is None:
        if path is None or not path.exists():
            raise ConfigError(f"config file not found: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"cannot read {path}: {e}") from e
    if path is not None:
        secrets.load_dotenv_files(path.parent / ".env", Path.cwd() / ".env")
    try:
        raw = _yaml_load(text) or {}
    except Exception as e:
        raise ConfigError("YAML parse error: " + " ".join(str(e).split())) from e
    if not isinstance(raw, dict):
        raise ConfigError("top level of config must be a mapping")

    cfg = AppConfig(path=path)
    cfg.warnings.extend(secrets.literal_secret_warnings(raw))
    unknown = set(raw) - {"settings", "channels", "alerts", "projects"}
    for k in sorted(unknown):
        cfg.errors.append(f"unknown top-level key '{k}'")

    try:
        cfg.settings = Settings.model_validate(raw.get("settings") or {})
    except ValidationError as e:
        cfg.errors.append(f"settings: {_fmt_validation(e)}")

    # named alert rules; 'default' is the base for everything
    rules_raw = raw.get("alerts") or {}
    if not isinstance(rules_raw, dict):
        cfg.errors.append("alerts: must be a mapping of rule-name -> rule")
        rules_raw = {}
    base_default = rules_raw.get("default") or {}
    for rname, rbody in {"default": base_default, **rules_raw}.items():
        try:
            merged = {**base_default, **(rbody or {})} if rname != "default" else (rbody or {})
            cfg.rules[rname] = AlertRule.model_validate(merged)
        except (ValidationError, TypeError) as e:
            msg = _fmt_validation(e) if isinstance(e, ValidationError) else str(e)
            cfg.errors.append(f"alerts.{rname}: {msg}")

    # channels
    for cname, cbody in (raw.get("channels") or {}).items():
        cfg.channels[cname] = _load_channel(cname, cbody, channel_registry, cfg)

    # projects
    projects_raw = raw.get("projects") or {}
    if not isinstance(projects_raw, dict):
        cfg.errors.append("projects: must be a mapping of project-name -> project")
        projects_raw = {}
    for pname, pbody in projects_raw.items():
        pbody = pbody or {}
        if not isinstance(pbody, dict):
            cfg.errors.append(f"projects.{pname}: must be a mapping")
            continue
        proj = ProjectSpec(name=str(pname), description=str(pbody.get("description") or ""),
                           enabled=bool(pbody.get("enabled", True)))
        pdeps = pbody.get("depends_on") or []
        if isinstance(pdeps, str):
            pdeps = [pdeps]
        if not isinstance(pdeps, list) or not all(isinstance(d, str) for d in pdeps):
            cfg.errors.append(f"projects.{pname}.depends_on: must be a list of names")
            pdeps = []
        proj.depends_on = pdeps
        try:
            proj_rule = _resolve_rule(pbody.get("alerts"), cfg.rules["default"], cfg)
        except ValueError as e:
            cfg.errors.append(f"projects.{pname}.alerts: {e}")
            proj_rule = cfg.rules["default"]
        seen: set[str] = set()
        for i, wbody in enumerate(pbody.get("watchers") or []):
            spec = _load_watcher(proj, i, wbody, proj_rule, watcher_registry, cfg)
            if spec.name in seen:
                spec.error = f"duplicate watcher name '{spec.name}' in project '{pname}'"
                cfg.errors.append(spec.error)
                spec.name = f"{spec.name}#{i}"
            seen.add(spec.name)
            if not proj.enabled:
                spec.enabled = False
            proj.watchers.append(spec)
        cfg.projects[proj.name] = proj

    for w in cfg.watchers():
        for ch in w.rule.channels + w.rule.escalate_channels:
            if ch not in cfg.channels:
                cfg.errors.append(f"{w.key}: alert rule references unknown channel '{ch}'")
    _resolve_dependencies(cfg)
    return cfg


def _resolve_dependencies(cfg: AppConfig) -> None:
    """``depends_on`` entries: 'project/watcher', 'watcher' (same project) or 'project'
    (= every watcher in it). Unknown names and cycles are config errors."""
    keys = {w.key for w in cfg.watchers()}
    for proj in cfg.projects.values():
        for w in proj.watchers:
            resolved: list[str] = []
            for ref in proj.depends_on + w.depends_on_raw:
                if "/" in ref:
                    targets = [ref] if ref in keys else []
                elif f"{proj.name}/{ref}" in keys:
                    targets = [f"{proj.name}/{ref}"]
                elif ref in cfg.projects:
                    targets = [x.key for x in cfg.projects[ref].watchers]
                else:
                    targets = []
                if not targets:
                    msg = f"depends_on: unknown watcher or project '{ref}'"
                    cfg.errors.append(f"{w.key}: {msg}")
                    w.error = w.error or msg
                    continue
                resolved += [t for t in targets if t != w.key and t not in resolved]
            w.depends_on = resolved
    # cycle detection (DFS, colors)
    graph = {w.key: w.depends_on for w in cfg.watchers()}
    state: dict[str, int] = {}
    in_cycle: set[str] = set()

    def visit(k: str, stack: list[str]) -> None:
        state[k] = 1
        stack.append(k)
        for d in graph.get(k, []):
            if state.get(d) == 1:
                in_cycle.update(stack[stack.index(d):])
            elif d not in state:
                visit(d, stack)
        stack.pop()
        state[k] = 2

    for k in graph:
        if k not in state:
            visit(k, [])
    for k in sorted(in_cycle):
        w = cfg.watcher(k)
        if w is not None:
            msg = "depends_on: dependency cycle"
            cfg.errors.append(f"{k}: {msg}")
            w.error = w.error or msg
            w.depends_on = []


def _resolve_rule(value: Any, base: AlertRule, cfg: AppConfig) -> AlertRule:
    if value is None:
        return base
    if isinstance(value, str):
        if value not in cfg.rules:
            raise ValueError(f"unknown alert rule '{value}'")
        return cfg.rules[value]
    if isinstance(value, dict):
        try:
            return AlertRule.model_validate({**base.model_dump(), **value})
        except ValidationError as e:
            raise ValueError(_fmt_validation(e)) from None
    raise ValueError("must be a rule name or a mapping")


def _load_watcher(proj: ProjectSpec, idx: int, body: Any, proj_rule: AlertRule,
                  reg: Registry[Watcher], cfg: AppConfig) -> WatcherSpec:
    where = f"projects.{proj.name}.watchers[{idx}]"
    if not isinstance(body, dict):
        spec = WatcherSpec(proj.name, f"#{idx}", "?", 0, 0, error=f"{where}: must be a mapping")
        cfg.errors.append(spec.error)
        return spec
    wtype = str(body.get("type") or "")
    name = str(body.get("name") or wtype or f"#{idx}")
    st = cfg.settings
    spec = WatcherSpec(project=proj.name, name=name, type=wtype, interval=st.default_interval,
                       timeout=st.default_timeout, rule=proj_rule)
    spec.options = {k: v for k, v in body.items() if k not in COMMON_KEYS}
    where = f"{proj.name}/{name}"

    def fail(msg: str) -> WatcherSpec:
        spec.error = msg
        cfg.errors.append(f"{where}: {msg}")
        return spec

    try:
        common = _Common.model_validate({k: v for k, v in body.items() if k in COMMON_KEYS})
    except ValidationError as e:
        return fail(_fmt_validation(e))
    spec.interval = common.interval or st.default_interval
    spec.timeout = common.timeout or st.default_timeout
    spec.retries, spec.retry_delay = common.retries, common.retry_delay
    spec.enabled, spec.description, spec.tags = common.enabled, common.description, common.tags
    spec.depends_on_raw = [common.depends_on] if isinstance(common.depends_on, str) else list(common.depends_on)
    try:
        spec.rule = _resolve_rule(common.alerts, proj_rule, cfg)
    except ValueError as e:
        return fail(f"alerts: {e}")

    if not wtype:
        return fail("missing 'type'")
    cls = reg.get(wtype)
    if cls is None:
        return fail(f"unknown watcher type '{wtype}' (available: {', '.join(sorted(reg.items))})")
    if common.interval is None:
        spec.interval = cls.default_interval
    missing: list[str] = []
    opts = secrets.expand(copy.deepcopy(spec.options), missing=missing)
    if missing:
        return fail(f"environment variable(s) not set: {', '.join(sorted(set(missing)))}")
    try:
        spec.config = cls.Config.model_validate(opts)
    except ValidationError as e:
        return fail(_fmt_validation(e))
    spec.unavailable = cls.unavailable_reason()
    if spec.unavailable:
        cfg.warnings.append(f"{where}: disabled - {spec.unavailable}")
    return spec


class _Common(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    type: str | None = None
    interval: Duration | None = Field(None, gt=0)
    timeout: Duration | None = Field(None, gt=0)
    retries: int = Field(0, ge=0, le=10)
    retry_delay: Duration = 2.0
    enabled: bool = True
    alerts: str | dict | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    depends_on: str | list[str] = Field(default_factory=list)
    on_alert: dict | None = None
    slo: float | None = Field(None, gt=0, lt=100)


def _load_channel(name: str, body: Any, reg: Registry[Channel], cfg: AppConfig) -> ChannelSpec:
    if not isinstance(body, dict):
        spec = ChannelSpec(name, "?", error="must be a mapping")
        cfg.errors.append(f"channels.{name}: {spec.error}")
        return spec
    ctype = str(body.get("type") or "")
    spec = ChannelSpec(name=name, type=ctype, ignore_quiet=bool(body.get("ignore_quiet", False)))
    opts = {k: v for k, v in body.items() if k not in {"type", "ignore_quiet", "min_severity"}}
    try:
        spec.min_severity = Status(str(body.get("min_severity", "WARN")).upper())
    except ValueError:
        spec.error = "min_severity must be WARN or ALERT"
    cls = reg.get(ctype)
    if spec.error:
        pass
    elif cls is None:
        spec.error = f"unknown channel type '{ctype}' (available: {', '.join(sorted(reg.items))})"
    else:
        missing: list[str] = []
        opts = secrets.expand(opts, missing=missing)
        if missing:
            spec.error = f"environment variable(s) not set: {', '.join(sorted(set(missing)))}"
        else:
            try:
                spec.config = cls.Config.model_validate(opts)
            except ValidationError as e:
                spec.error = _fmt_validation(e)
            spec.unavailable = cls.unavailable_reason()
    if spec.error:
        cfg.errors.append(f"channels.{name}: {spec.error}")
    elif spec.unavailable:
        cfg.warnings.append(f"channels.{name}: disabled - {spec.unavailable}")
    return spec


# --------------------------------------------------------------------------- editing
# Round-trip edits keep the user's comments and ordering.

def _rt() -> YAML:
    y = YAML()  # round-trip
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _read_rt(path: Path) -> Any:
    if not path.exists():
        return _rt().load("projects: {}\n")
    data = _rt().load(path.read_text(encoding="utf-8"))
    return data if data is not None else _rt().load("projects: {}\n")


def _write_rt(path: Path, data: Any) -> None:
    buf = io.StringIO()
    _rt().dump(data, buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    os.replace(tmp, path)


def add_project(path: Path, project: str, description: str = "") -> None:
    data = _read_rt(path)
    if data.get("projects") is None:
        data["projects"] = {}
    if project in data["projects"]:
        raise ConfigError(f"project '{project}' already exists")
    body: dict[str, Any] = {}
    if description:
        body["description"] = description
    body["watchers"] = []
    data["projects"][project] = body
    _write_rt(path, data)


def upsert_watcher(path: Path, project: str, body: dict[str, Any], *, original_name: str | None = None) -> None:
    """Add a watcher (or replace ``original_name``) in ``project``; creates the project if needed."""
    data = _read_rt(path)
    if data.get("projects") is None:
        data["projects"] = {}
    projects = data["projects"]
    if project not in projects or projects[project] is None:
        projects[project] = {"watchers": []}
    proj = projects[project]
    if proj.get("watchers") is None:
        proj["watchers"] = []
    watchers = proj["watchers"]
    target = original_name or body["name"]
    for i, w in enumerate(watchers):
        if isinstance(w, dict) and str(w.get("name") or w.get("type")) == target:
            if original_name is None:
                raise ConfigError(f"watcher '{target}' already exists in '{project}'")
            existing = w
            for k in list(existing.keys()):
                if k not in body:
                    del existing[k]
            for k, v in body.items():
                existing[k] = v
            break
    else:
        if original_name is not None:
            raise ConfigError(f"watcher '{original_name}' not found in '{project}'")
        watchers.append(body)
    _write_rt(path, data)


def set_watcher_enabled(path: Path, project: str, name: str, enabled: bool) -> None:
    data = _read_rt(path)
    for w in (data.get("projects") or {}).get(project, {}).get("watchers") or []:
        if isinstance(w, dict) and str(w.get("name") or w.get("type")) == name:
            if enabled and "enabled" in w:
                del w["enabled"]
            elif not enabled:
                w["enabled"] = False
            _write_rt(path, data)
            return
    raise ConfigError(f"watcher {project}/{name} not found")


def raw_watcher(path: Path, project: str, name: str) -> dict[str, Any] | None:
    try:
        data = _yaml_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    for w in ((data.get("projects") or {}).get(project) or {}).get("watchers") or []:
        if isinstance(w, dict) and str(w.get("name") or w.get("type")) == name:
            return dict(w)
    return None


STARTER_CONFIG = """\
# kwatchdog config. Secrets: reference env vars as ${NAME}; put values in
# ~/.watchdog/.env or the real environment - never in this file.
settings:
  heartbeat_port: 8787        # dead-man's-switch endpoint: GET /ping/<name>

channels:
  bell:
    type: bell
  # desktop:
  #   type: desktop
  # phone:
  #   type: ntfy
  #   topic: ${NTFY_TOPIC}
  #   ignore_quiet: true

alerts:
  default:
    severity: WARN
    min_failures: 2
    cooldown: 10m
    # quiet_hours: "23:00-07:00"
    # escalate_after: 30m

projects:
  local:
    description: This machine
    watchers:
      - name: disk
        type: disk
        path: /
        warn_percent: 85
        alert_percent: 95
      - name: internet
        type: ping
        host: 1.1.1.1
        interval: 30s
"""
