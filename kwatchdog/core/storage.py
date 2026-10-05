"""SQLite store shared by the daemon (writer) and TUI clients (readers).

WAL mode lets a separate TUI process read while the daemon writes. TUI ->
daemon actions (run now, mute, reload, ...) go through the ``commands`` table,
which works the same whether the TUI is embedded or a separate process.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import Result, Status
from .secrets import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    wkey TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    latency_ms REAL,
    metrics TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_results_key_ts ON results(wkey, ts);

CREATE TABLE IF NOT EXISTS watcher_state (
    wkey TEXT PRIMARY KEY,
    status TEXT,
    message TEXT,
    last_check REAL,
    latency_ms REAL,
    muted_until REAL,
    disabled INTEGER DEFAULT 0,
    flapping INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    wkey TEXT NOT NULL,
    opened REAL NOT NULL,
    closed REAL,
    status TEXT,
    message TEXT,
    escalated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_incidents_key ON incidents(wkey, opened);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    wkey TEXT,
    kind TEXT,
    status TEXT,
    message TEXT,
    delivered TEXT
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    target TEXT,
    arg TEXT,
    done REAL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS heartbeats (
    name TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS remediation_runs (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    wkey TEXT NOT NULL,
    action TEXT NOT NULL,
    command TEXT,
    mode TEXT NOT NULL,      -- run | dry-run | pending | rate-limited | rejected | off
    exit_code INTEGER,
    output TEXT,
    duration_ms REAL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS ix_runs_key_ts ON remediation_runs(wkey, ts);
"""


@dataclass
class WatcherRow:
    key: str
    status: Status
    message: str
    last_check: float | None
    latency_ms: float | None
    muted_until: float | None
    disabled: bool
    flapping: bool

    def muted(self, now: float | None = None) -> bool:
        return bool(self.muted_until and self.muted_until > (now or time.time()))


@dataclass
class ResultRow:
    id: int
    ts: float
    key: str
    status: Status
    message: str
    latency_ms: float | None
    metrics: dict[str, float]
    raw: str


@dataclass
class IncidentRow:
    id: int
    key: str
    opened: float
    closed: float | None
    status: Status
    message: str
    escalated: bool


@dataclass
class EventRow:
    id: int
    ts: float
    key: str
    kind: str
    status: str
    message: str
    delivered: str


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def _x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, tuple(args))

    # ----------------------------------------------------------------- results
    def record_result(self, key: str, r: Result, *, keep_raw: int = 4000) -> None:
        raw = redact(r.raw or "")[:keep_raw]
        msg = redact(r.message or "")
        self._x(
            "INSERT INTO results (ts, wkey, status, message, latency_ms, metrics, raw) VALUES (?,?,?,?,?,?,?)",
            (r.ts, key, r.status.value, msg, r.latency_ms, json.dumps(r.metrics) if r.metrics else None, raw),
        )
        self._x(
            """INSERT INTO watcher_state (wkey, status, message, last_check, latency_ms)
               VALUES (?,?,?,?,?)
               ON CONFLICT(wkey) DO UPDATE SET status=excluded.status, message=excluded.message,
                 last_check=excluded.last_check, latency_ms=excluded.latency_ms""",
            (key, r.status.value, msg, r.ts, r.latency_ms),
        )

    def set_status(self, key: str, status: Status, message: str) -> None:
        """Status without a check (sleeping / config error). Doesn't touch last_check."""
        self._x(
            """INSERT INTO watcher_state (wkey, status, message) VALUES (?,?,?)
               ON CONFLICT(wkey) DO UPDATE SET status=excluded.status, message=excluded.message""",
            (key, status.value, redact(message)),
        )

    def results(self, key: str, limit: int = 50) -> list[ResultRow]:
        rows = self._x("SELECT * FROM results WHERE wkey=? ORDER BY ts DESC LIMIT ?", (key, limit)).fetchall()
        return [self._result(r) for r in rows]

    def first_result_ts(self, key: str, since: float) -> float | None:
        row = self._x("SELECT MIN(ts) FROM results WHERE wkey=? AND ts>=?", (key, since)).fetchone()
        return row[0] if row and row[0] is not None else None

    def latencies(self, key: str, limit: int = 60) -> list[float]:
        rows = self._x(
            "SELECT latency_ms FROM results WHERE wkey=? AND latency_ms IS NOT NULL ORDER BY ts DESC LIMIT ?",
            (key, limit),
        ).fetchall()
        return [r[0] for r in reversed(rows)]

    def all_latencies(self, limit: int = 40) -> dict[str, list[float]]:
        """Latest ``limit`` latencies for every watcher (one query, for the main view)."""
        rows = self._x(
            """SELECT wkey, latency_ms FROM (
                 SELECT wkey, latency_ms, ts, ROW_NUMBER() OVER (PARTITION BY wkey ORDER BY ts DESC) rn
                 FROM results WHERE latency_ms IS NOT NULL) WHERE rn <= ? ORDER BY wkey, ts""",
            (limit,),
        ).fetchall()
        out: dict[str, list[float]] = {}
        for k, v in rows:
            out.setdefault(k, []).append(v)
        return out

    def metric_history(self, key: str, metric: str, limit: int = 50) -> list[float]:
        rows = self._x(
            "SELECT metrics FROM results WHERE wkey=? AND metrics IS NOT NULL ORDER BY ts DESC LIMIT ?",
            (key, limit * 3),
        ).fetchall()
        vals = []
        for (m,) in rows:
            try:
                d = json.loads(m)
            except ValueError:
                continue
            if metric in d and d[metric] is not None:
                vals.append(float(d[metric]))
            if len(vals) >= limit:
                break
        return list(reversed(vals))

    @staticmethod
    def _result(r: sqlite3.Row) -> ResultRow:
        return ResultRow(r["id"], r["ts"], r["wkey"], Status(r["status"]), r["message"] or "",
                         r["latency_ms"], json.loads(r["metrics"]) if r["metrics"] else {}, r["raw"] or "")

    def stats(self, key: str, since: float) -> dict[str, Any]:
        rows = self._x("SELECT status, latency_ms FROM results WHERE wkey=? AND ts>=?", (key, since)).fetchall()
        # uptime = share of checks not in ALERT; SLEEPING/BLOCKED aren't the watcher's own verdict
        counted = [r for r in rows if r[0] not in (Status.SLEEPING.value, Status.BLOCKED.value)]
        alerts = sum(1 for r in counted if r[0] == Status.ALERT.value)
        lats = sorted(r[1] for r in rows if r[1] is not None)
        return {
            "checks": len(counted),
            "uptime": (100.0 * (len(counted) - alerts) / len(counted)) if counted else None,
            "p50": _pct(lats, 50),
            "p95": _pct(lats, 95),
            "warn": sum(1 for r in counted if r[0] == Status.WARN.value),
            "alert": sum(1 for r in counted if r[0] == Status.ALERT.value),
        }

    # ------------------------------------------------------------ watcher state
    def watcher_rows(self) -> dict[str, WatcherRow]:
        out = {}
        for r in self._x("SELECT * FROM watcher_state").fetchall():
            out[r["wkey"]] = WatcherRow(r["wkey"], Status(r["status"] or "SLEEPING"), r["message"] or "",
                                        r["last_check"], r["latency_ms"], r["muted_until"],
                                        bool(r["disabled"]), bool(r["flapping"]))
        return out

    def watcher_row(self, key: str) -> WatcherRow | None:
        return self.watcher_rows().get(key)

    def _ensure(self, key: str) -> None:
        self._x("INSERT OR IGNORE INTO watcher_state (wkey, status) VALUES (?, 'SLEEPING')", (key,))

    def set_muted(self, key: str, until: float | None) -> None:
        self._ensure(key)
        self._x("UPDATE watcher_state SET muted_until=? WHERE wkey=?", (until, key))

    def set_disabled(self, key: str, disabled: bool) -> None:
        self._ensure(key)
        self._x("UPDATE watcher_state SET disabled=? WHERE wkey=?", (int(disabled), key))

    def set_flapping(self, key: str, flapping: bool) -> None:
        self._ensure(key)
        self._x("UPDATE watcher_state SET flapping=? WHERE wkey=?", (int(flapping), key))

    def forget_watchers(self, keep: set[str]) -> None:
        for k in list(self.watcher_rows()):
            if k not in keep:
                self._x("DELETE FROM watcher_state WHERE wkey=?", (k,))

    # ---------------------------------------------------------------- incidents
    def open_incident(self, key: str, ts: float, status: Status, message: str) -> int:
        cur = self._x("INSERT INTO incidents (wkey, opened, status, message) VALUES (?,?,?,?)",
                      (key, ts, status.value, redact(message)))
        return int(cur.lastrowid)

    def update_incident(self, iid: int, status: Status, message: str, escalated: bool | None = None) -> None:
        self._x("UPDATE incidents SET status=?, message=? WHERE id=?", (status.value, redact(message), iid))
        if escalated is not None:
            self._x("UPDATE incidents SET escalated=? WHERE id=?", (int(escalated), iid))

    def close_incident(self, iid: int, ts: float) -> None:
        self._x("UPDATE incidents SET closed=? WHERE id=?", (ts, iid))

    def incidents(self, key: str | None = None, limit: int = 50) -> list[IncidentRow]:
        if key:
            rows = self._x("SELECT * FROM incidents WHERE wkey=? ORDER BY opened DESC LIMIT ?", (key, limit))
        else:
            rows = self._x("SELECT * FROM incidents ORDER BY opened DESC LIMIT ?", (limit,))
        return [IncidentRow(r["id"], r["wkey"], r["opened"], r["closed"], Status(r["status"]),
                            r["message"] or "", bool(r["escalated"])) for r in rows.fetchall()]

    # ------------------------------------------------------------------- events
    def add_event(self, key: str, kind: str, status: str, message: str, delivered: str = "", ts: float | None = None) -> None:
        self._x("INSERT INTO events (ts, wkey, kind, status, message, delivered) VALUES (?,?,?,?,?,?)",
                (ts or time.time(), key, kind, status, redact(message), delivered))

    def events(self, limit: int = 100, after_id: int = 0) -> list[EventRow]:
        rows = self._x("SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?", (after_id, limit)).fetchall()
        return [EventRow(r["id"], r["ts"], r["wkey"] or "", r["kind"], r["status"], r["message"] or "",
                         r["delivered"] or "") for r in rows]

    # ----------------------------------------------------------------- commands
    def push_command(self, kind: str, target: str = "", arg: str = "") -> None:
        self._x("INSERT INTO commands (ts, kind, target, arg) VALUES (?,?,?,?)", (time.time(), kind, target, arg))

    def take_commands(self) -> list[tuple[str, str, str]]:
        rows = self._x("SELECT id, kind, target, arg FROM commands WHERE done IS NULL ORDER BY id").fetchall()
        if rows:
            self._x(f"UPDATE commands SET done=? WHERE id IN ({','.join('?' * len(rows))})",
                    (time.time(), *[r[0] for r in rows]))
        return [(r[1], r[2] or "", r[3] or "") for r in rows]

    # ----------------------------------------------------------------------- kv
    def kv_get(self, k: str, default: Any = None) -> Any:
        row = self._x("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, k: str, v: Any) -> None:
        self._x("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # --------------------------------------------------------------- heartbeats
    def beat(self, name: str, ts: float | None = None) -> None:
        self._x("""INSERT INTO heartbeats (name, ts, count) VALUES (?,?,1)
                   ON CONFLICT(name) DO UPDATE SET ts=excluded.ts, count=count+1""", (name, ts or time.time()))

    def heartbeat_last(self, name: str) -> float | None:
        row = self._x("SELECT ts FROM heartbeats WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------- maintenance
    # ----------------------------------------------------------- remediation
    def add_run(self, key: str, action: str, command: str, mode: str, ts: float | None = None) -> int:
        cur = self._x("INSERT INTO remediation_runs (ts, wkey, action, command, mode) VALUES (?,?,?,?,?)",
                      (ts or time.time(), key, action, redact(command), mode))
        return int(cur.lastrowid)

    def finish_run(self, rid: int, exit_code: int | None, output: str, duration_ms: float,
                   mode: str | None = None) -> None:
        self._x("UPDATE remediation_runs SET exit_code=?, output=?, duration_ms=?, finished=? WHERE id=?",
                (exit_code, redact(output)[-4000:], duration_ms, time.time(), rid))
        if mode:
            self._x("UPDATE remediation_runs SET mode=? WHERE id=?", (mode, rid))

    def set_run_mode(self, rid: int, mode: str) -> None:
        self._x("UPDATE remediation_runs SET mode=? WHERE id=?", (mode, rid))

    def runs(self, key: str | None = None, limit: int = 50) -> list[dict]:
        if key:
            rows = self._x("SELECT * FROM remediation_runs WHERE wkey=? ORDER BY id DESC LIMIT ?", (key, limit))
        else:
            rows = self._x("SELECT * FROM remediation_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows.fetchall()]

    def run(self, rid: int) -> dict | None:
        row = self._x("SELECT * FROM remediation_runs WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

    def count_runs(self, key: str, since: float, modes: tuple[str, ...]) -> int:
        q = f"SELECT COUNT(*) FROM remediation_runs WHERE wkey=? AND ts>=? AND mode IN ({','.join('?' * len(modes))})"
        return int(self._x(q, (key, since, *modes)).fetchone()[0])

    def last_run(self, key: str, modes: tuple[str, ...]) -> dict | None:
        q = f"SELECT * FROM remediation_runs WHERE wkey=? AND mode IN ({','.join('?' * len(modes))}) ORDER BY id DESC LIMIT 1"
        row = self._x(q, (key, *modes)).fetchone()
        return dict(row) if row else None

    def prune(self, retention_days: int) -> None:
        # results feed month-to-date uptime budgets: keep at least 35 days of them
        self._x("DELETE FROM results WHERE ts < ?", (time.time() - max(retention_days, 35) * 86400,))
        cutoff = time.time() - retention_days * 86400
        self._x("DELETE FROM remediation_runs WHERE ts < ?", (cutoff,))
        self._x("DELETE FROM events WHERE ts < ?", (cutoff,))
        self._x("DELETE FROM commands WHERE done IS NOT NULL AND done < ?", (time.time() - 3600,))
        self._x("DELETE FROM incidents WHERE closed IS NOT NULL AND closed < ?", (cutoff,))


def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    q = statistics.quantiles(sorted_vals, n=100, method="inclusive")
    return q[int(p) - 1]
