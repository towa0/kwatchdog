import datetime as dt

import pytest

from kwatchdog.core.alerts import AlertEngine, AlertState, in_quiet_hours
from kwatchdog.core.config import AlertRule
from kwatchdog.core.models import Result, Status

T0 = 1_700_000_000.0


def run(engine, st, rule, statuses, start=T0, step=60, muted=False):
    """Feed a sequence like 'OAAWO' (O=OK W=WARN A=ALERT); return actions per step."""
    out = []
    for i, ch in enumerate(statuses):
        s = {"O": Status.OK, "W": Status.WARN, "A": Status.ALERT}[ch]
        out.append(engine.process(st, Result(s, f"msg-{ch}", ts=start + i * step), rule, muted=muted))
    return out


def notified(actions):
    return [a.kind for step in actions for a in step if a.notify]


def noon(ts):  # deterministic local clock for tests: every ts is 12:00
    return dt.datetime(2024, 1, 1, 12, 0)


@pytest.fixture
def engine():
    return AlertEngine(localtime=noon)


def test_min_consecutive_failures(engine):
    rule = AlertRule(min_failures=3, cooldown=0)
    st = AlertState()
    acts = run(engine, st, rule, "AAOAAA")
    # two failures then OK: no incident; then three in a row -> one alert
    assert notified(acts) == ["alert"]
    assert [a.kind for a in acts[5] if a.notify] == ["alert"]
    assert st.incident_open


def test_dedup_and_recovery(engine):
    rule = AlertRule(min_failures=1, cooldown=0)
    st = AlertState()
    acts = run(engine, st, rule, "AAAAO")
    assert notified(acts) == ["alert", "recovery"]  # repeats are deduped
    close = [a for a in acts[4] if a.kind == "close"][0]
    assert close.duration_s == 4 * 60
    assert not st.incident_open


def test_severity_threshold(engine):
    rule = AlertRule(severity=Status.ALERT, cooldown=0)
    st = AlertState()
    acts = run(engine, st, rule, "WWWAO")
    assert notified(acts) == ["alert", "recovery"]
    assert [a.kind for a in acts[3] if a.notify] == ["alert"]


def test_worsening_sends_update(engine):
    rule = AlertRule(cooldown=0)
    st = AlertState()
    assert notified(run(engine, st, rule, "WWA")) == ["alert", "update"]


def test_no_recovery_if_never_notified(engine):
    rule = AlertRule(severity=Status.ALERT, cooldown=0)
    st = AlertState()
    assert notified(run(engine, st, rule, "WWO")) == []


def test_cooldown_suppresses_then_sends_when_expired(engine):
    rule = AlertRule(cooldown=600, flap_window=20)
    st = AlertState()
    # alert at t0, recover at +60 (recovery bypasses cooldown? it's sent, sets last_notified)
    acts = run(engine, st, rule, "AO")
    assert notified(acts) == ["alert", "recovery"]
    # new failure 2 min later: within cooldown -> held back
    acts2 = run(engine, st, rule, "AA", start=T0 + 180)
    assert notified(acts2) == []
    # still failing after the cooldown expires -> alert goes out
    acts3 = run(engine, st, rule, "A", start=T0 + 60 + 601)
    assert notified(acts3) == ["alert"]


def test_flapping_detected_and_damped(engine):
    rule = AlertRule(cooldown=0, flap_window=6, flap_threshold=0.6)
    st = AlertState()
    acts = run(engine, st, rule, "AOAOAOAOAO")
    kinds = notified(acts)
    assert "flapping" in kinds
    first_flap = kinds.index("flapping")
    # after the flapping notice, no more alert/recovery spam
    assert kinds[first_flap + 1:] == []
    assert st.flapping
    # stable again -> flap ends, alerts resume
    acts = run(engine, st, rule, "OOOOOO", start=T0 + 10_000)
    assert notified(acts) == ["flap_end"]
    assert not st.flapping
    assert notified(run(engine, st, rule, "A", start=T0 + 20_000)) == ["alert"]


def test_escalation_once_after_n_minutes(engine):
    rule = AlertRule(cooldown=0, escalate_after=30 * 60, escalate_channels=["phone"], channels=["bell"])
    st = AlertState()
    acts = run(engine, st, rule, "A" * 40, step=60)
    esc = [a for step in acts for a in step if a.kind == "escalation"]
    assert len(esc) == 1
    assert esc[0].channels == ["phone"]
    assert acts[0][-1].channels == ["bell"]


def test_escalation_via_tick_without_new_results(engine):
    rule = AlertRule(cooldown=0, escalate_after=600)
    st = AlertState()
    run(engine, st, rule, "A")
    assert engine.tick(st, rule, T0 + 300) == []
    acts = engine.tick(st, rule, T0 + 601)
    assert [a.kind for a in acts] == ["escalation"]
    assert engine.tick(st, rule, T0 + 1200) == []


def test_mute_suppresses_until_unmuted(engine):
    rule = AlertRule(cooldown=0)
    st = AlertState()
    assert notified(run(engine, st, rule, "AA", muted=True)) == []
    assert st.incident_open  # incident still tracked
    assert notified(run(engine, st, rule, "A", start=T0 + 500)) == ["alert"]


def test_quiet_hours_flag_and_held_delivery():
    clock = {"h": 23}
    engine = AlertEngine(localtime=lambda ts: dt.datetime(2024, 1, 1, clock["h"], 30))
    rule = AlertRule(cooldown=0, quiet_hours="23:00-07:00")
    st = AlertState()
    acts = run(engine, st, rule, "A")
    alert = [a for a in acts[0] if a.notify][0]
    assert alert.quiet and st.held
    clock["h"] = 8
    acts = engine.tick(st, rule, T0 + 9 * 3600)
    assert [a.kind for a in acts] == ["alert"] and not acts[0].quiet
    assert "held during quiet hours" in acts[0].message


def test_quiet_hours_parsing():
    at = lambda h, m: (lambda ts: dt.datetime(2024, 1, 1, h, m))  # noqa: E731
    assert in_quiet_hours("23:00-07:00", 0, at(23, 0))
    assert in_quiet_hours("23:00-07:00", 0, at(3, 0))
    assert not in_quiet_hours("23:00-07:00", 0, at(7, 0))
    assert in_quiet_hours("09:00-17:00", 0, at(12, 0))
    assert not in_quiet_hours("09:00-17:00", 0, at(18, 0))
    assert not in_quiet_hours(None, 0)


def test_sleeping_results_ignored(engine):
    rule = AlertRule(cooldown=0)
    st = AlertState()
    assert engine.process(st, Result.sleeping("x", ts=T0), rule) == []
    assert st.history == []


def test_state_roundtrip():
    st = AlertState(consecutive_failures=3, history=["OK", "ALERT"], incident_open=True)
    assert AlertState.from_dict(st.to_dict()) == st
    assert AlertState.from_dict({"bogus": 1}) == AlertState()
