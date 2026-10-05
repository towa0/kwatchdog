"""Core value types shared by daemon, watchers, alerting and the TUI."""
from __future__ import annotations

import enum
import re
import time
from dataclasses import dataclass, field
from typing import Any, Annotated

from pydantic import BeforeValidator


class Status(str, enum.Enum):
    """Status vocabulary. Ordered by severity via ``rank``."""

    OK = "OK"
    WARN = "WARN"
    ALERT = "ALERT"
    SLEEPING = "SLEEPING"  # disabled, muted, unavailable or not yet checked
    BLOCKED = "BLOCKED"  # failing, but a dependency is in ALERT: the root cause alerts instead

    @property
    def rank(self) -> int:
        return {"SLEEPING": 0, "OK": 1, "BLOCKED": 2, "WARN": 3, "ALERT": 4}[self.value]

    @property
    def failing(self) -> bool:
        return self in (Status.WARN, Status.ALERT)

    @classmethod
    def worst(cls, statuses) -> "Status":
        statuses = list(statuses)
        if not statuses:
            return cls.SLEEPING
        return max(statuses, key=lambda s: s.rank)


@dataclass
class Result:
    status: Status
    message: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    raw: str = ""
    latency_ms: float | None = None
    ts: float = field(default_factory=time.time)

    @classmethod
    def ok(cls, message: str = "ok", **kw: Any) -> "Result":
        return cls(Status.OK, message, **kw)

    @classmethod
    def warn(cls, message: str, **kw: Any) -> "Result":
        return cls(Status.WARN, message, **kw)

    @classmethod
    def alert(cls, message: str, **kw: Any) -> "Result":
        return cls(Status.ALERT, message, **kw)

    @classmethod
    def sleeping(cls, message: str, **kw: Any) -> "Result":
        return cls(Status.SLEEPING, message, **kw)


_DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$", re.I)
_DUR_MULT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def parse_duration(value: Any) -> float:
    """'90' / 90 -> 90s, '5m' -> 300, '1.5h' -> 5400, '2d' -> 172800."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _DUR_RE.match(value)
        if m:
            unit = m.group(2).lower() if m.group(2) else None
            return float(m.group(1)) * _DUR_MULT[unit]
    raise ValueError(f"invalid duration {value!r} (use e.g. 30, '30s', '5m', '2h', '1d')")


Duration = Annotated[float, BeforeValidator(parse_duration)]


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s < 600 else f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"

