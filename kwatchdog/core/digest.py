"""Daily digest + monthly uptime budgets (SLOs).

Digest: once a day at ``digest.at`` (local time; caught up on startup if the
daemon was down at that time), sent through the existing channels. It covers
incidents since the previous digest, 24h uptime per project, flapping
watchers, things that are silently stale, budgets and autofix activity.

Budgets: a watcher (or its project) with ``slo: 99.9`` gets a monthly
downtime budget of (1 - 99.9%) of the month. Downtime so far is estimated from
month-to-date uptime. The rest of the month is projected at the burn rate of
the last ``settings.slo_lookback`` (default 24h). If the projection exceeds
the budget, an ALERT goes out, at most once per day per watcher and again if
it gets worse (will-miss -> exhausted).
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Status, fmt_age

if TYPE_CHECKING:
    from .config import AppConfig
    from .storage import Store

LocalTime = Callable[[float], dt.datetime]


class DigestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    at: str = "07:30"  # local time
    channels: list[str] = Field(default_factory=list)  # [] = every channel except bell
    stale_factor: float = Field(3.0, ge=1.5)  # "silently stale": no check for N x interval
    max_lines: int = Field(60, ge=10)

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        try:
            h, m = (int(x) for x in v.split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            raise ValueError("at must look like '07:30'") from None
        return f"{h:02d}:{m:02d}"


# ------------------------------------------------------------------- budgets
@dataclass
class Budget:
    key: str
    slo: float
    state: str  # ok | will-miss | exhausted | no-data
    mtd_uptime: float | None
    projected_uptime: float | None
    budget_s: float  # allowed downtime this month, seconds
    used_s: float  # estimated downtime so far
    burn_rate: float | None  # recent error rate / allowed error rate (1.0 = exactly on budget)

    @property
    def used_pct(self) -> float:
        return 100.0 * self.used_s / self.budget_s if self.budget_s else 0.0

    def describe(self) -> str:
        if self.state == "no-data":
            return f"SLO {self.slo:g}%: not enough data yet"
        proj = f"{self.projected_uptime:.3f}%" if self.projected_uptime is not None else "?"
        burn = f", burn rate {self.burn_rate:.1f}x" if self.burn_rate is not None else ""
        head = {"ok": "on track", "will-miss": "WILL MISS", "exhausted": "BUDGET EXHAUSTED"}[self.state]
        return (f"SLO {self.slo:g}% {head}: projected {proj} this month, "
                f"{self.used_pct:.0f}% of {fmt_age(self.budget_s)} budget used{burn}")


def month_bounds(now: float, localtime: LocalTime | None = None) -> tuple[float, float]:
    t = (localtime or dt.datetime.fromtimestamp)(now)
    start = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start.timestamp(), end.timestamp()


def evaluate_budget(store: "Store", key: str, slo: float, now: float, lookback: float = 86400,
                    localtime: LocalTime | None = None, min_checks: int = 5) -> Budget:
    start, end = month_bounds(now, localtime)
    total, elapsed, remaining = end - start, max(0.0, now - start), max(0.0, end - now)
    allowed = 1.0 - slo / 100.0
    budget_s = allowed * total
    mtd = store.stats(key, start)
    recent = store.stats(key, now - lookback)
    if mtd["checks"] < min_checks or mtd["uptime"] is None:
        return Budget(key, slo, "no-data", None, None, budget_s, 0.0, None)
    used_s = (1.0 - mtd["uptime"] / 100.0) * elapsed
    recent_bad = (1.0 - recent["uptime"] / 100.0) if recent["checks"] and recent["uptime"] is not None \
        else (1.0 - mtd["uptime"] / 100.0)
    projected_down = used_s + recent_bad * remaining
    projected_uptime = 100.0 * (1.0 - projected_down / total)
    burn = recent_bad / allowed if allowed > 0 else None
    if used_s > budget_s:
        state = "exhausted"
    elif projected_down > budget_s:
        state = "will-miss"
    else:
        state = "ok"
    return Budget(key, slo, state, mtd["uptime"], projected_uptime, budget_s, used_s, burn)


# -------------------------------------------------------------------- digest
def build_digest(cfg: "AppConfig", store: "Store", now: float, since: float,
                 dcfg: DigestConfig | None = None, localtime: LocalTime | None = None,
                 daemon_started: float | None = None) -> tuple[Status, str]:
    """(overall status, text). Pure read of config + store."""
    from .remediation import get_mode

    dcfg = dcfg or DigestConfig()
    lt = localtime or dt.datetime.fromtimestamp
    rows = store.watcher_rows()
    specs = cfg.watchers()
    lines: list[str] = []
    statuses = []
    for w in specs:
        r = rows.get(w.key)
        statuses.append(Status.SLEEPING if r is None or r.disabled else r.status)
    overall = Status.worst(statuses)
    counts = {s: statuses.count(s) for s in Status if statuses.count(s)}
    lines.append(f"kwatchdog digest · {lt(now):%a %d %b %H:%M}")
    lines.append("now: " + " · ".join(f"{n} {s.value}" for s, n in sorted(counts.items(), key=lambda x: -x[0].rank)))

    # incidents since the last digest
    keys = {w.key for w in specs}
    incs = [i for i in store.incidents(limit=500) if i.opened >= since and i.key in keys]
    span = fmt_age(now - since)
    if incs:
        lines.append(f"\nINCIDENTS last {span} ({len(incs)})")
        for i in sorted(incs, key=lambda i: i.opened):
            end = f"{lt(i.closed):%H:%M}" if i.closed else "still open"
            dur = fmt_age((i.closed or now) - i.opened)
            lines.append(f"  {i.status.value:<5} {i.key} {lt(i.opened):%H:%M}-{end} ({dur})"
                         f"{' [escalated]' if i.escalated else ''}: {i.message[:90]}")
    else:
        lines.append(f"\nINCIDENTS last {span}: none")

    # uptime per project (24h)
    parts = []
    for p in cfg.projects.values():
        ups = [s["uptime"] for s in (store.stats(w.key, now - 86400) for w in p.watchers) if s["uptime"] is not None]
        parts.append(f"{p.name} {sum(ups) / len(ups):.2f}%" if ups else f"{p.name} -")
    if parts:
        lines.append("\nUPTIME 24h: " + " · ".join(parts))

    # flapping (now, or a flapping notice in the period)
    flapping = {k for k, r in rows.items() if r.flapping and k in keys}
    flapping |= {e.key for e in store.events(500) if e.kind == "flapping" and e.ts >= since and e.key in keys}
    if flapping:
        lines.append("\nFLAPPING: " + ", ".join(sorted(flapping)))

    # silently stale: things that look fine in a glance but aren't doing their job
    stale: list[str] = []
    up_for = now - daemon_started if daemon_started else None
    for w in specs:
        r = rows.get(w.key)
        if w.error:
            stale.append(f"{w.key}: config error - {w.error[:80]}")
        elif w.unavailable:
            stale.append(f"{w.key}: not running - {w.unavailable[:80]}")
        elif not w.enabled:
            stale.append(f"{w.key}: disabled in config")
        elif r is not None and r.disabled:
            stale.append(f"{w.key}: disabled from the TUI/CLI")
        elif r is not None and r.muted(now) and (r.muted_until or 0) - now > 86400:
            stale.append(f"{w.key}: muted for {fmt_age((r.muted_until or now) - now)} more")
        elif r is None or r.last_check is None:
            if up_for is None or up_for > dcfg.stale_factor * w.interval:
                stale.append(f"{w.key}: never checked")
        elif now - r.last_check > dcfg.stale_factor * w.interval:
            stale.append(f"{w.key}: last check {fmt_age(now - r.last_check)} ago (interval {fmt_age(w.interval)})")
    if stale:
        lines.append(f"\nSILENTLY STALE ({len(stale)})")
        lines += [f"  {s}" for s in stale]

    # budgets
    budget_lines = []
    for w in specs:
        if w.slo:
            b = evaluate_budget(store, w.key, w.slo, now, cfg.settings.slo_lookback, localtime)
            if b.state != "no-data":
                mark = {"ok": "ok ", "will-miss": "!! ", "exhausted": "XX "}[b.state]
                budget_lines.append(f"  {mark}{w.key}: {b.describe()}")
    if budget_lines:
        lines.append("\nUPTIME BUDGETS (month)")
        lines += budget_lines

    # autofix
    runs = [r for r in store.runs(limit=500) if r["ts"] >= since]
    mode = get_mode(store) if cfg.settings.autofix else "off (config)"
    if runs or mode != "on":
        failed = sum(1 for r in runs if r["mode"] == "run" and r["exit_code"] not in (0, None))
        pending = sum(1 for r in runs if r["mode"] == "pending")
        lines.append(f"\nAUTOFIX ({mode}): {len(runs)} attempt(s), {failed} failed, {pending} awaiting confirm")

    errors = store.kv_get("config_errors", []) or []
    if errors:
        lines.append(f"\nCONFIG ERRORS ({len(errors)}): " + "; ".join(e[:80] for e in errors[:3]))

    out = "\n".join(lines).splitlines()
    if len(out) > dcfg.max_lines:
        out = out[: dcfg.max_lines - 1] + [f"… {len(out) - dcfg.max_lines + 1} more line(s) (watchdog digest)"]
    return overall, "\n".join(out)


# --------------------------------------------------------------- scheduling
class Digester:
    """Time-driven part, called from the daemon's ticker."""

    LAST = "digest_last"
    BUDGET_CHECK = "budget_last_check"

    def __init__(self, daemon, localtime: LocalTime | None = None):
        self.d = daemon
        self.localtime = localtime or dt.datetime.fromtimestamp

    def due(self, now: float) -> bool:
        dcfg: DigestConfig = self.d.config.digest
        if not dcfg.enabled:
            return False
        t = self.localtime(now)
        h, m = (int(x) for x in dcfg.at.split(":"))
        if (t.hour, t.minute) < (h, m):
            return False
        last = self.d.store.kv_get(self.LAST) or {}
        return last.get("date") != t.strftime("%Y-%m-%d")

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.due(now):
            await self.send_digest(now)
        last = self.d.store.kv_get(self.BUDGET_CHECK) or 0
        if now - last >= 3600:
            self.d.store.kv_set(self.BUDGET_CHECK, now)
            await self.check_budgets(now)

    async def send_digest(self, now: float) -> str:
        store = self.d.store
        last = store.kv_get(self.LAST) or {}
        since = float(last.get("ts") or now - 86400)
        overall, text = build_digest(self.d.config, store, now, since, self.d.config.digest, self.localtime,
                                     self.d.started)
        store.kv_set(self.LAST, {"date": self.localtime(now).strftime("%Y-%m-%d"), "ts": now})
        names = self.d.config.digest.channels or [n for n, c in self.d.config.channels.items() if c.type != "bell"]
        await self.d.send_to(names, "digest", "kwatchdog", "daily", overall, text, key="")
        return text

    async def check_budgets(self, now: float) -> None:
        from .alerts import Action

        store = self.d.store
        today = self.localtime(now).strftime("%Y-%m-%d")
        severity = {"ok": 0, "no-data": 0, "will-miss": 1, "exhausted": 2}
        for spec in list(self.d.specs.values()):
            if not spec.slo or not spec.runnable:
                continue
            b = evaluate_budget(store, spec.key, spec.slo, now, self.d.config.settings.slo_lookback, self.localtime)
            store.kv_set(f"budget:{spec.key}", {"state": b.state, "text": b.describe(), "ts": now})
            if severity[b.state] == 0:
                continue
            sent = store.kv_get(f"budget_alert:{spec.key}") or {}
            if sent.get("date") == today and severity.get(sent.get("state"), 0) >= severity[b.state]:
                continue
            store.kv_set(f"budget_alert:{spec.key}", {"date": today, "state": b.state})
            await self.d.dispatch(spec, Action("budget", Status.ALERT, b.describe(), notify=True,
                                               channels=list(spec.rule.channels)))
