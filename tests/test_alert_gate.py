"""Managed alert delivery: one alert per incident, however hard it flaps.

On the platform this came from, a flapping connection once produced 330+
texts. These assert the thing that was actually wanted — that an outage is
announced once, a recovery once, and that no pattern of toggling gets past
that. `AlertGate` is pure arithmetic on a fake clock, so every rule is tested
in isolation and instantly.
"""
import pytest

from app import alerts as alert_state
from app.alerts import ALERT, RECOVERY, AlertGate

KEY = ("snapshot_stale", "prod")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(clock):
    return AlertGate(clock=clock)


def poll(gate, clock, key, healthy, seconds, step=30, deliver=True, **kw):
    """Report `healthy` every `step` seconds for `seconds`. Returns the
    (time, action) of everything the gate told us to send, and — like the real
    caller — reports each one as sent."""
    out = []
    for _ in range(int(seconds // step)):
        v = gate.observe(key, healthy, **kw)
        if v.action:
            out.append((clock.t, v.action))
            if deliver:
                gate.sent(key, v.action, texted=True)
        clock.advance(step)
    return out


# ── the state machine ──────────────────────────────────────────────────────

def test_going_bad_alerts_exactly_once(gate, clock):
    sent = poll(gate, clock, KEY, False, 330 * 120, step=120)
    assert [a for _, a in sent] == [ALERT], "330 bad polls, one alert"


def test_a_stay_in_the_bad_state_is_suppressed_with_a_reason(gate, clock):
    assert gate.observe(KEY, False).action == ALERT
    gate.sent(KEY, ALERT)
    v = gate.observe(KEY, False)
    assert v.action is None and "already alerted" in v.reason


def test_a_healthy_thing_never_alerts_or_recovers(gate, clock):
    assert poll(gate, clock, KEY, True, 3600) == []


def test_recovery_fires_once_after_the_debounce(gate, clock):
    poll(gate, clock, KEY, False, 300)                  # a 5 minute outage
    sent = poll(gate, clock, KEY, True, 600)
    assert [a for _, a in sent] == [RECOVERY]
    assert poll(gate, clock, KEY, True, 3600) == [], "and never a second one"


def test_recovery_waits_for_the_debounce(gate, clock):
    poll(gate, clock, KEY, False, 300)
    assert gate.observe(KEY, True).action is None, "one good reading is not a recovery"
    clock.advance(30)
    assert gate.observe(KEY, True).action is None
    clock.advance(30)
    assert gate.observe(KEY, True).action == RECOVERY


def test_a_blip_of_good_inside_the_debounce_changes_nothing(gate, clock):
    """The flap: down, up for 30 seconds, down. That is one outage."""
    poll(gate, clock, KEY, False, 300)
    gate.observe(KEY, True)
    clock.advance(30)
    assert gate.observe(KEY, False).action is None, "no second alert"
    assert gate.state(KEY) == alert_state.UNHEALTHY
    # ...and the recovery clock restarted: 59 seconds of good is still not back.
    gate.observe(KEY, True)
    clock.advance(59)
    assert gate.observe(KEY, True).action is None


def test_no_recovery_for_an_outage_nobody_was_told_about(gate, clock):
    """If the alert never went out (transport down, throttled), an all-clear
    would announce an incident the reader never heard of."""
    poll(gate, clock, KEY, False, 300, deliver=False)
    assert poll(gate, clock, KEY, True, 600, deliver=False) == []


def test_no_recovery_for_an_outage_shorter_than_the_minimum(clock):
    g = AlertGate(clock=clock, debounce_s=10, min_outage_s=60)
    assert g.observe(KEY, False).action == ALERT
    g.sent(KEY, ALERT)
    clock.advance(20)
    g.observe(KEY, True)
    clock.advance(20)
    v = g.observe(KEY, True)
    assert v.action is None and "minimum" in v.reason


def test_down_debounce_ignores_a_short_bad_reading(gate, clock):
    """A socket that says "not connected" for thirty seconds is not an outage."""
    assert poll(gate, clock, KEY, False, 30, down_debounce_s=60) == []
    assert poll(gate, clock, KEY, True, 60, down_debounce_s=60) == []
    assert poll(gate, clock, KEY, False, 60, down_debounce_s=60) == [], "the wait restarted"
    assert poll(gate, clock, KEY, True, 30, down_debounce_s=60) == []
    sent = poll(gate, clock, KEY, False, 240, down_debounce_s=60)
    assert [a for _, a in sent] == [ALERT], "held for a minute, then once"


def test_a_failed_send_is_offered_again_but_not_before_the_retry_delay(gate, clock):
    assert gate.observe(KEY, False).action == ALERT       # caller fails, never calls sent()
    clock.advance(30)
    assert gate.observe(KEY, False).action is None, "too soon to retry"
    clock.advance(31)
    assert gate.observe(KEY, False).action == ALERT
    gate.sent(KEY, ALERT)
    clock.advance(600)
    assert gate.observe(KEY, False).action is None, "sent, so never again"


def test_a_new_outage_inside_the_cooldown_does_not_alert(clock):
    g = AlertGate(clock=clock, cooldown_s=600)
    poll(g, clock, KEY, False, 120)                       # alert, t=0
    poll(g, clock, KEY, True, 120)                        # recovery
    sent = poll(g, clock, KEY, False, 240)                # down again, ~4 min later
    assert sent == [], "cooldown"
    sent = poll(g, clock, KEY, False, 600)                # still down after the cooldown
    assert [a for _, a in sent] == [ALERT], "still down afterwards, so tell them"


def test_the_ceiling_is_three_per_thirty_minutes(clock):
    """Nothing gets past it: no cooldown, so only the ceiling is on trial."""
    g = AlertGate(clock=clock, cooldown_s=0)
    times = []
    for _ in range(40):
        times += [t for t, _a in poll(g, clock, KEY, False, 120)]
        times += [t for t, _a in poll(g, clock, KEY, True, 120)]
    assert times, "the gate is not simply refusing everything"
    for t in times:
        in_window = [x for x in times if t <= x < t + 1800]
        assert len(in_window) <= 3, f"{len(in_window)} alerts inside 30 minutes"


def test_the_ceiling_lifts_once_the_window_has_passed(clock):
    g = AlertGate(clock=clock, cooldown_s=0)
    for _ in range(3):
        g.observe(KEY, False)
        g.sent(KEY, ALERT)
        clock.advance(61)
        g.observe(KEY, True)
        clock.advance(61)
        g.observe(KEY, True)
        clock.advance(1)
    assert g.sent_count(KEY) >= 3
    assert g.observe(KEY, False).action is None
    clock.advance(1900)
    assert g.observe(KEY, False).action == ALERT


def test_incidents_are_counted_apart(gate, clock):
    other = ("snapshot_stale", "staging")
    assert gate.observe(KEY, False).action == ALERT
    assert gate.observe(other, False).action == ALERT, "another environment is its own incident"
    assert gate.observe(("ai_unreachable", "prod"), False).action == ALERT


def test_forgetting_a_scope_ends_its_incidents_without_a_recovery(gate, clock):
    poll(gate, clock, KEY, False, 300)
    gate.forget("prod")
    assert gate.state(KEY) == alert_state.HEALTHY
    assert poll(gate, clock, KEY, True, 600) == []
