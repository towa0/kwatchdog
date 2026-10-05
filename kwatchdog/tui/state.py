"""Read-side model for the TUI: config structure + live state from SQLite."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text

from ..core.config import AppConfig, ConfigError, load_config
from ..core.daemon import CONFIG_ERRORS, CONFIG_WARNINGS, daemon_alive
from ..core.models import Status
from ..core.plugin import registries
from ..core.storage import EventRow, Store, WatcherRow

STATUS_STYLE = {
    Status.ALERT: "bold #000000 on #ff1a1a",
    Status.WARN: "bold #ff1a1a",
    Status.OK: "#8b0000",
    Status.SLEEPING: "#5f5f5f",
}
STATUS_GLYPH = {Status.ALERT: "!!", Status.WARN: "! ", Status.OK: "--", Status.SLEEPING: "zz"}
BLOCKS = "▁▂▃▄▅▆▇█"


def status_text(status: Status, width: int = 8) -> Text:
    return Text(f" {status.value:<{width - 1}}", style=STATUS_STYLE[status])


def sparkline(values: list[float], width: int = 20) -> str:
    vals = values[-width:]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    return "".join(BLOCKS[min(7, int((v - lo) / span * 7.99))] for v in vals)


def fuzzy_score(query: str, text: str) -> int | None:
    """Subsequence match (fzf-ish). None = no match; higher = better."""
    if not query:
        return 0
    q, t = query.lower(), text.lower()
    if q in t:
        return 1000 - t.index(q)
    score, ti, streak = 0, 0, 0
    for ch in q:
        found = t.find(ch, ti)
        if found < 0:
            return None
        streak = streak + 1 if found == ti else 0
        score += 10 + streak * 5 - (found - ti)
        ti = found + 1
    return score


@dataclass
class Snapshot:
    config: AppConfig
    rows: dict[str, WatcherRow]
    latencies: dict[str, list[float]]
    events: list[EventRow]
    daemon: dict | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def status(self, key: str) -> Status:
        row = self.rows.get(key)
        if row is None or row.disabled:
            return Status.SLEEPING
        return row.status

    def effective(self, key: str) -> Status:
        """Status as displayed: muted watchers show SLEEPING unless failing."""
        return self.status(key)

    def project_status(self, project: str) -> Status:
        p = self.config.projects.get(project)
        return Status.worst(self.status(w.key) for w in p.watchers) if p else Status.SLEEPING

    def counts(self) -> dict[Status, int]:
        out = {s: 0 for s in Status}
        for w in self.config.watchers():
            out[self.status(w.key)] += 1
        return out


class StateReader:
    def __init__(self, config_path: Path, store: Store | None = None):
        self.config_path = config_path
        self.config = AppConfig(path=config_path)
        self.local_errors: list[str] = []
        self._mtime: float | None = None
        self.load_config()
        self.store = store or Store(self.config.settings.path("db"))

    def load_config(self, force: bool = False) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            self.local_errors = [f"config file not found: {self.config_path}"]
            return
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            wreg, creg = registries(self.config.settings.path("plugins_dir"))
            self.config = load_config(self.config_path, wreg, creg)
            self.local_errors = []
        except ConfigError as e:
            self.local_errors = [str(e)]

    def snapshot(self) -> Snapshot:
        self.load_config()
        daemon = daemon_alive(self.store)
        if daemon:
            errors = self.store.kv_get(CONFIG_ERRORS, []) or []
            warnings = self.store.kv_get(CONFIG_WARNINGS, []) or []
        else:
            errors = self.local_errors + self.config.errors
            warnings = self.config.warnings
        return Snapshot(
            config=self.config,
            rows=self.store.watcher_rows(),
            latencies=self.store.all_latencies(40),
            events=self.store.events(60),
            daemon=daemon,
            errors=list(errors),
            warnings=list(warnings),
        )
