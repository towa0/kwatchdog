"""Shared helpers for watchers: thresholds, JSON paths, source fetching, timestamps."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..core.models import Status


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warn_above: float | None = None
    alert_above: float | None = None
    warn_below: float | None = None
    alert_below: float | None = None

    def any(self) -> bool:
        return any(v is not None for v in (self.warn_above, self.alert_above, self.warn_below, self.alert_below))


def evaluate(value: float, t: Thresholds, label: str = "value", unit: str = "") -> tuple[Status, str]:
    v = f"{value:g}{unit}"
    if t.alert_above is not None and value > t.alert_above:
        return Status.ALERT, f"{label} {v} > {t.alert_above:g}{unit}"
    if t.alert_below is not None and value < t.alert_below:
        return Status.ALERT, f"{label} {v} < {t.alert_below:g}{unit}"
    if t.warn_above is not None and value > t.warn_above:
        return Status.WARN, f"{label} {v} > {t.warn_above:g}{unit}"
    if t.warn_below is not None and value < t.warn_below:
        return Status.WARN, f"{label} {v} < {t.warn_below:g}{unit}"
    return Status.OK, f"{label} {v}"


_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[(-?\d+)\]")


def json_path(data: Any, path: str) -> Any:
    """'data.items[0].price' or 'data.items.0.price'. Raises KeyError with context."""
    if not path or path == "$":
        return data
    path = path[2:] if path.startswith("$.") else path
    cur = data
    for m in _PATH_TOKEN.finditer(path):
        tok = m.group(1) if m.group(1) is not None else m.group(0)
        try:
            if isinstance(cur, list):
                cur = cur[int(tok)]
            elif isinstance(cur, dict):
                cur = cur[tok]
            else:
                raise KeyError(tok)
        except (KeyError, IndexError, ValueError):
            raise KeyError(f"path '{path}' not found at '{tok}'") from None
    return cur


def to_number(v: Any) -> float:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = re.sub(r"[^\d.,eE+-]", "", v.strip())
        if "," in s and "." in s:  # the last separator is the decimal one
            thousands = "," if s.rfind(",") < s.rfind(".") else "."
            s = s.replace(thousands, "").replace(",", ".")
        elif s.count(",") == 1 and len(s.split(",")[1]) != 3:
            s = s.replace(",", ".")  # 3,5 -> 3.5
        elif "," in s or s.count(".") > 1:
            s = s.replace("," if "," in s else ".", "")  # 1,234 / 1.234.567 -> thousands
        return float(s)
    raise ValueError(f"not a number: {v!r}")


def to_timestamp(v: Any) -> float:
    """Epoch seconds/ms or ISO-8601 -> epoch seconds."""
    if isinstance(v, (int, float)):
        return float(v) / 1000 if v > 1e12 else float(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            return to_timestamp(float(s))
        except ValueError:
            pass
        s = s.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.astimezone()  # naive = local time
        return d.timestamp()
    if isinstance(v, dt.datetime):
        return v.timestamp()
    raise ValueError(f"not a timestamp: {v!r}")


async def fetch_text(ctx, url: str | None, file: str | None, *, timeout: float = 10.0,
                     headers: dict[str, str] | None = None) -> str:
    if url:
        r = await ctx.http().get(url, timeout=timeout, headers=headers or {}, follow_redirects=True)
        r.raise_for_status()
        return r.text
    if file:
        return Path(file).expanduser().read_text(encoding="utf-8")
    raise ValueError("need 'url' or 'file'")


async def fetch_json(ctx, url: str | None, file: str | None, **kw: Any) -> Any:
    return json.loads(await fetch_text(ctx, url, file, **kw))
