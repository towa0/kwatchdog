"""Data watchers: scraper health, SQLite/CSV freshness + anomaly, JSON metric, price."""
from __future__ import annotations

import asyncio
import csv
import json
import re
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from ..core.models import Duration, Result, Status, fmt_age
from ..core.plugin import Watcher, WatcherConfig
from ._common import Thresholds, evaluate, fetch_json, fetch_text, json_path, to_number, to_timestamp


def zscore(value: float, history: list[float]) -> float | None:
    if len(history) < 3:
        return None
    mean = statistics.fmean(history)
    sd = statistics.pstdev(history)
    if sd == 0:
        return 0.0 if value == mean else float("inf") * (1 if value > mean else -1)
    return (value - mean) / sd


def _sqlite_scalar(db: str, sql: str) -> Any:
    uri = f"file:{Path(db).expanduser().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        row = con.execute(sql).fetchone()
        return row[0] if row else None
    finally:
        con.close()


class ScraperConfig(WatcherConfig):
    # source A: a SQLite DB the scraper writes to
    db: str | None = None
    rows_query: str | None = None  # SELECT COUNT(*) FROM items
    last_success_query: str | None = None  # SELECT MAX(scraped_at) FROM runs WHERE ok=1
    errors_query: str | None = None
    requests_query: str | None = None
    # source B: a JSON status file / URL the scraper publishes
    status_file: str | None = None
    status_url: str | None = None
    rows_key: str = "rows"
    last_success_key: str = "last_success"
    errors_key: str = "errors"
    requests_key: str = "requests"
    baseline_runs: int = Field(10, ge=2)
    warn_drop_percent: float = 30
    alert_drop_percent: float = 60
    min_rows: float | None = None
    max_success_age: Duration | None = None
    error_rate_warn: float = 0.05
    error_rate_alert: float = 0.25

    @model_validator(mode="after")
    def _src(self):
        if not (self.db or self.status_file or self.status_url):
            raise ValueError("set 'db' (+queries) or 'status_file' / 'status_url'")
        return self


class ScraperWatcher(Watcher):
    type = "scraper"
    description = "scraper health: row delta vs baseline, last-success age, error rate"
    Config = ScraperConfig
    default_interval = 300

    async def _collect(self) -> dict[str, Any]:
        c: ScraperConfig = self.config
        if c.db:
            def q() -> dict[str, Any]:
                return {k: _sqlite_scalar(c.db, sql) for k, sql in (
                    ("rows", c.rows_query), ("last_success", c.last_success_query),
                    ("errors", c.errors_query), ("requests", c.requests_query)) if sql}
            return await asyncio.to_thread(q)
        data = await fetch_json(self.ctx, c.status_url, c.status_file, timeout=self.timeout)
        out = {}
        for k, path in (("rows", c.rows_key), ("last_success", c.last_success_key),
                        ("errors", c.errors_key), ("requests", c.requests_key)):
            try:
                out[k] = json_path(data, path)
            except KeyError:
                pass
        return out

    async def check(self) -> Result:
        c: ScraperConfig = self.config
        try:
            d = await self._collect()
        except Exception as e:
            return Result.alert(f"cannot read scraper status: {e}")
        problems: list[tuple[Status, str]] = []
        metrics: dict[str, float] = {}
        msg_parts = []
        if d.get("rows") is not None:
            rows = to_number(d["rows"])
            metrics["rows"] = rows
            msg_parts.append(f"{rows:g} rows")
            hist = self.ctx.history("rows", c.baseline_runs)
            if len(hist) >= 2:
                base = statistics.median(hist)
                if base > 0:
                    delta = 100.0 * (rows - base) / base
                    metrics["delta_percent"] = round(delta, 1)
                    msg_parts.append(f"{delta:+.0f}% vs baseline {base:g}")
                    if -delta >= c.alert_drop_percent:
                        problems.append((Status.ALERT, f"rows dropped {-delta:.0f}% vs baseline {base:g}"))
                    elif -delta >= c.warn_drop_percent:
                        problems.append((Status.WARN, f"rows dropped {-delta:.0f}% vs baseline {base:g}"))
            if c.min_rows is not None and rows < c.min_rows:
                problems.append((Status.ALERT, f"rows {rows:g} < {c.min_rows:g}"))
        if d.get("last_success") is not None:
            age = time.time() - to_timestamp(d["last_success"])
            metrics["success_age_s"] = round(age, 1)
            msg_parts.append(f"last ok {fmt_age(age)} ago")
            if c.max_success_age and age > c.max_success_age:
                problems.append((Status.ALERT, f"no successful run for {fmt_age(age)}"))
        elif c.max_success_age:
            problems.append((Status.ALERT, "no successful run recorded"))
        if d.get("errors") is not None and d.get("requests"):
            rate = to_number(d["errors"]) / max(to_number(d["requests"]), 1)
            metrics["error_rate"] = round(rate, 4)
            msg_parts.append(f"err {rate:.1%}")
            if rate >= c.error_rate_alert:
                problems.append((Status.ALERT, f"error rate {rate:.1%}"))
            elif rate >= c.error_rate_warn:
                problems.append((Status.WARN, f"error rate {rate:.1%}"))
        raw = json.dumps(d, default=str, indent=2)
        if problems:
            return Result(Status.worst(p[0] for p in problems), "; ".join(p[1] for p in problems), metrics, raw)
        return Result.ok(", ".join(msg_parts) or "ok", metrics=metrics, raw=raw)


class DataFileConfig(WatcherConfig):
    path: str
    format: str | None = None  # sqlite | csv (guessed from extension)
    table: str | None = None  # sqlite
    timestamp_column: str | None = None  # newest value = freshness; else file mtime
    max_age: Duration | None = None
    min_rows: int | None = None
    warn_z: float = 3.0
    alert_z: float = 5.0
    min_history: int = Field(5, ge=3)

    @model_validator(mode="after")
    def _fmt(self):
        if self.format is None:
            ext = Path(self.path).suffix.lower()
            self.format = "csv" if ext in (".csv", ".tsv", ".txt") else "sqlite"
        if self.format not in ("csv", "sqlite"):
            raise ValueError("format must be 'csv' or 'sqlite'")
        if self.format == "sqlite" and not self.table:
            raise ValueError("sqlite needs 'table'")
        if self.table and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.table):
            raise ValueError("table must be a plain identifier")
        if self.timestamp_column and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ ]*", self.timestamp_column):
            raise ValueError("timestamp_column must be a plain identifier")
        return self


class DataFileWatcher(Watcher):
    type = "datafile"
    description = "SQLite/CSV freshness + row-count anomaly (z-score vs history)"
    Config = DataFileConfig
    default_interval = 300

    def _read(self) -> tuple[int, float | None]:
        c: DataFileConfig = self.config
        p = Path(c.path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"{c.path} does not exist")
        newest: float | None = None
        if c.format == "sqlite":
            rows = int(_sqlite_scalar(str(p), f'SELECT COUNT(*) FROM "{c.table}"') or 0)
            if c.timestamp_column:
                v = _sqlite_scalar(str(p), f'SELECT MAX("{c.timestamp_column}") FROM "{c.table}"')
                newest = to_timestamp(v) if v is not None else None
        else:
            delim = "\t" if p.suffix.lower() == ".tsv" else ","
            rows = 0
            with p.open(newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f, delimiter=delim)
                for row in reader:
                    rows += 1
                    if c.timestamp_column and row.get(c.timestamp_column):
                        try:
                            ts = to_timestamp(row[c.timestamp_column])
                            newest = ts if newest is None else max(newest, ts)
                        except ValueError:
                            pass
        if newest is None and not c.timestamp_column:
            newest = p.stat().st_mtime
        return rows, newest

    async def check(self) -> Result:
        c: DataFileConfig = self.config
        try:
            rows, newest = await asyncio.to_thread(self._read)
        except Exception as e:
            return Result.alert(f"cannot read: {e}")
        metrics: dict[str, float] = {"rows": float(rows)}
        problems: list[tuple[Status, str]] = []
        parts = [f"{rows} rows"]
        if newest is not None:
            age = time.time() - newest
            metrics["age_s"] = round(age, 1)
            parts.append(f"newest {fmt_age(age)} ago")
            if c.max_age and age > c.max_age:
                problems.append((Status.ALERT, f"stale: newest data {fmt_age(age)} old"))
        elif c.max_age:
            problems.append((Status.ALERT, "no timestamps found"))
        hist = self.ctx.history("rows", 50)
        if len(hist) >= c.min_history:
            z = zscore(rows, hist)
            if z is not None:
                metrics["z"] = round(z, 2) if abs(z) != float("inf") else 99.0
                if abs(z) >= c.alert_z:
                    problems.append((Status.ALERT, f"row count anomaly: {rows} (z={z:+.1f})"))
                elif abs(z) >= c.warn_z:
                    problems.append((Status.WARN, f"row count unusual: {rows} (z={z:+.1f})"))
        if c.min_rows is not None and rows < c.min_rows:
            problems.append((Status.ALERT, f"{rows} rows < {c.min_rows}"))
        if problems:
            return Result(Status.worst(p[0] for p in problems), "; ".join(p[1] for p in problems), metrics)
        return Result.ok(", ".join(parts), metrics=metrics)


class JsonMetricConfig(Thresholds):
    url: str | None = None
    file: str | None = None
    path: str = "$"  # e.g. data.queue.depth  or  items[0].value
    headers: dict[str, str] = Field(default_factory=dict)
    label: str | None = None

    @model_validator(mode="after")
    def _src(self):
        if not (self.url or self.file):
            raise ValueError("set 'url' or 'file'")
        return self


class JsonMetricWatcher(Watcher):
    type = "json"
    description = "number from JSON (URL/file) vs thresholds - the generic escape hatch"
    Config = JsonMetricConfig
    default_interval = 60

    async def check(self) -> Result:
        c: JsonMetricConfig = self.config
        try:
            data = await fetch_json(self.ctx, c.url, c.file, timeout=self.timeout, headers=c.headers)
            value = to_number(json_path(data, c.path))
        except Exception as e:
            return Result.alert(f"cannot read metric: {e}")
        status, msg = evaluate(value, c, c.label or c.path)
        return Result(status, msg, {"value": value}, raw=json.dumps(data, default=str)[:2000])


class PriceConfig(Thresholds):
    url: str | None = None
    file: str | None = None
    path: str | None = None  # JSON path (if the source is JSON)
    regex: str | None = None  # or: first group of this regex on the text
    headers: dict[str, str] = Field(default_factory=dict)
    symbol: str = "price"
    cross_above: float | None = None  # ALERT on the check where value crosses up through this
    cross_below: float | None = None
    change_window: int = Field(10, ge=1)  # compare with value N checks ago
    change_warn_percent: float | None = None
    change_alert_percent: float | None = None

    @model_validator(mode="after")
    def _src(self):
        if not (self.url or self.file):
            raise ValueError("set 'url' or 'file'")
        if not (self.path or self.regex):
            self.path = "$"
        return self


class PriceWatcher(Watcher):
    type = "price"
    description = "price/number threshold, level crossings and % change (trading signals)"
    Config = PriceConfig
    default_interval = 60

    async def check(self) -> Result:
        c: PriceConfig = self.config
        try:
            text = await fetch_text(self.ctx, c.url, c.file, timeout=self.timeout, headers=c.headers)
            if c.regex:
                m = re.search(c.regex, text)
                if not m:
                    raise ValueError(f"regex /{c.regex}/ did not match")
                value = to_number(m.group(1) if m.groups() else m.group(0))
            else:
                value = to_number(json_path(json.loads(text), c.path or "$"))
        except Exception as e:
            return Result.alert(f"cannot read {c.symbol}: {e}")
        hist = self.ctx.history("value", c.change_window)
        prev = hist[-1] if hist else None
        parts: list[tuple[Status, str]] = [evaluate(value, c, c.symbol)]
        metrics = {"value": value}
        if prev is not None:
            if c.cross_above is not None and prev <= c.cross_above < value:
                parts.append((Status.ALERT, f"crossed above {c.cross_above:g}"))
            if c.cross_below is not None and prev >= c.cross_below > value:
                parts.append((Status.ALERT, f"crossed below {c.cross_below:g}"))
        if len(hist) >= c.change_window and hist[0]:
            chg = 100.0 * (value - hist[0]) / abs(hist[0])
            metrics["change_percent"] = round(chg, 3)
            if c.change_alert_percent is not None and abs(chg) >= c.change_alert_percent:
                parts.append((Status.ALERT, f"{chg:+.2f}% over {c.change_window} checks"))
            elif c.change_warn_percent is not None and abs(chg) >= c.change_warn_percent:
                parts.append((Status.WARN, f"{chg:+.2f}% over {c.change_window} checks"))
        status = Status.worst(p[0] for p in parts)
        msg = "; ".join(p[1] for p in parts if p[0] != Status.OK) if status != Status.OK else parts[0][1]
        if status != Status.OK and not msg.startswith(c.symbol):
            msg = f"{c.symbol} {value:g}: {msg}"
        return Result(status, msg, metrics, raw=text[:2000])
