"""Alert pipeline: turns a stream of check results into incidents and notifications.

Pure logic (time is injected) so every rule is unit-testable:

* ``min_failures``  - N consecutive failing checks before an incident opens
* ``severity``      - minimum status (WARN/ALERT) that notifies
* flap damping      - too many OK<->failing changes in the last ``flap_window``
                      checks => one FLAPPING notice, then alerts and recoveries are
                      suppressed until stable, which sends one "stable again" notice
* ``cooldown``      - minimum time between notifications for one watcher; an
                      alert suppressed by cooldown is sent later if still failing
* dedup             - one incident = one alert; repeats of the same failure are
                      silent, only a severity increase (WARN -> ALERT) re-notifies
* ``quiet_hours``   - notifications are flagged ``quiet`` (only channels with
                      ``ignore_quiet`` get them); held alerts go out when quiet ends
* ``escalate_after``- incident open this long => one escalation notice
* recovery          - incident closes on the first OK; recovery notice if alerted
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Callable

from .config import AlertRule, parse_quiet_hours
from .models import Result, Status


@dataclass
class AlertState:
    consecutive_failures: int = 0
    history: list[str] = field(default_factory=list)
    flapping: bool = False
    incident_open: bool = False
    incident_id: int | None = None
    opened: float | None = None
    status: str | None = None  # current incident status
    message: str = ""
    notified: bool = False  # an alert for this incident went out
    notified_status: str | None = None
    held: bool = False  # alert went out during quiet hours only; resend after
    escalated: bool = False
    last_notified: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "AlertState":
        if not d:
            return cls()
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Action:
    kind: str  # notify kinds: alert|update|escalation|recovery|flapping ; incident ops: open|close|change|flap_end
    status: Status
    message: str
    notify: bool = False
    channels: list[str] = field(default_factory=list)  # [] = all channels
    quiet: bool = False
    duration_s: float | None = None


def in_quiet_hours(spec: str | None, now: float, localtime: Callable[[float], dt.datetime] | None = None) -> bool:
    if not spec:
        return False
    start, end = parse_quiet_hours(spec)
    t = (localtime or dt.datetime.fromtimestamp)(now)
    m = t.hour * 60 + t.minute
    if start == end:
        return False
    if start < end:
        return start <= m < end
    return m >= start or m < end  # wraps midnight


class AlertEngine:
    def __init__(self, localtime: Callable[[float], dt.datetime] | None = None):
        self.localtime = localtime

    # ------------------------------------------------------------------ helpers
    def _quiet(self, rule: AlertRule, now: float) -> bool:
        return in_quiet_hours(rule.quiet_hours, now, self.localtime)

    @staticmethod
    def _cooldown_ok(st: AlertState, rule: AlertRule, now: float) -> bool:
        return st.last_notified is None or now - st.last_notified >= rule.cooldown

    def _notify(self, st: AlertState, rule: AlertRule, now: float, kind: str, status: Status,
                message: str, channels: list[str] | None = None, duration: float | None = None) -> Action:
        quiet = self._quiet(rule, now)
        st.last_notified = now
        if kind in ("alert", "update") and quiet:
            st.held = True
        return Action(kind, status, message, notify=True, channels=list(channels if channels is not None else rule.channels),
                      quiet=quiet, duration_s=duration)

    # --------------------------------------------------------------------- main
    def process(self, st: AlertState, r: Result, rule: AlertRule, now: float | None = None,
                *, muted: bool = False) -> list[Action]:
        now = r.ts if now is None else now
        if r.status == Status.SLEEPING:
            return []
        acts: list[Action] = []

        # flap detection: OK <-> failing changes over the full window (WARN<->ALERT isn't a flap)
        st.history = (st.history + [r.status.value])[-rule.flap_window:]
        fails = [Status(s).failing for s in st.history]
        changes = sum(1 for a, b in zip(fails, fails[1:]) if a != b)
        rate = changes / (rule.flap_window - 1)
        if not st.flapping and rate >= rule.flap_threshold:
            st.flapping = True
            msg = f"flapping: {changes} state changes in last {len(st.history)} checks; alerts paused until stable"
            if muted:
                acts.append(Action("flapping", r.status, msg))
            elif self._cooldown_ok(st, rule, now):
                acts.append(self._notify(st, rule, now, "flapping", r.status, msg))
            else:
                acts.append(Action("flapping", r.status, msg))
        elif st.flapping and rate <= rule.flap_threshold / 2:
            st.flapping = False
            msg = f"stable again, now {r.status.value}: {r.message}"
            acts.append(self._notify(st, rule, now, "flap_end", r.status, msg) if not muted
                        else Action("flap_end", r.status, msg))

        if r.status.failing:
            st.consecutive_failures += 1
            if not st.incident_open and st.consecutive_failures >= rule.min_failures:
                st.incident_open, st.opened = True, now
                st.status, st.message = r.status.value, r.message
                st.notified, st.notified_status, st.held, st.escalated = False, None, False, False
                acts.append(Action("open", r.status, r.message))
            elif st.incident_open and (st.status != r.status.value or st.message != r.message):
                st.status, st.message = r.status.value, r.message
                acts.append(Action("change", r.status, r.message))
            if st.incident_open:
                acts.extend(self._incident_notifications(st, r.status, r.message, rule, now, muted))
        else:  # OK
            st.consecutive_failures = 0
            if st.incident_open:
                duration = now - (st.opened or now)
                acts.append(Action("close", r.status, r.message, duration_s=duration))
                if st.notified and rule.recovery and not muted and not st.flapping:
                    acts.append(self._notify(st, rule, now, "recovery", Status.OK,
                                             f"recovered: {r.message}", duration=duration))
                st.incident_open = False
                st.incident_id = None
                st.opened = st.status = None
                st.message = ""
                st.notified, st.notified_status, st.held, st.escalated = False, None, False, False
        acts.extend(self.tick(st, rule, now, muted=muted, _from_process=True))
        return acts

    def _incident_notifications(self, st: AlertState, status: Status, message: str, rule: AlertRule,
                                now: float, muted: bool) -> list[Action]:
        if muted or st.flapping or status.rank < rule.severity.rank:
            return []
        if not st.notified:
            if self._cooldown_ok(st, rule, now):
                st.notified, st.notified_status = True, status.value
                return [self._notify(st, rule, now, "alert", status, message)]
            return []
        if st.notified_status and status.rank > Status(st.notified_status).rank and self._cooldown_ok(st, rule, now):
            st.notified_status = status.value
            return [self._notify(st, rule, now, "update", status, f"worsened to {status.value}: {message}")]
        return []

    def tick(self, st: AlertState, rule: AlertRule, now: float, *, muted: bool = False,
             _from_process: bool = False) -> list[Action]:
        """Time-based transitions (escalation, held quiet-hour alerts). The daemon
        calls this periodically so escalation doesn't depend on check interval."""
        acts: list[Action] = []
        if not st.incident_open or muted:
            return acts
        status = Status(st.status or "ALERT")
        quiet = self._quiet(rule, now)
        if st.held and not quiet:
            st.held = False
            st.last_notified = now
            acts.append(Action("alert", status, f"(held during quiet hours) {st.message}", notify=True,
                               channels=list(rule.channels)))
        if (rule.escalate_after and not st.escalated and st.opened is not None
                and now - st.opened >= rule.escalate_after and status.rank >= rule.severity.rank
                and not st.flapping):
            st.escalated = True
            chans = rule.escalate_channels or rule.channels
            acts.append(self._notify(st, rule, now, "escalation", status,
                                     f"still {status.value} after {int((now - st.opened) // 60)} min: {st.message}",
                                     channels=chans, duration=now - st.opened))
        return acts
