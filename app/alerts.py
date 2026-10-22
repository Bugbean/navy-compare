"""When an alert may go out — the state machine that stops a flapping check
from becoming a flood of messages.

A checker reports a condition every run ("the snapshot is stale", "the AI is
not answering"). Sending on every report turns one flapping check into
hundreds of messages in an evening. This module turns a stream of
observations into at most a handful of *decisions*.

    healthy   -> unhealthy   one alert
    unhealthy -> unhealthy   nothing (however many polls)
    unhealthy -> healthy     one recovery, if it was a real outage we told
                             someone about

Around that, three limits, each one a reason a rule could still be noisy:

  debounce     A change of state only counts once the new state has held for
               `debounce_s`. A connection that toggles inside that window
               never leaves the state it was in, so it is one change, not
               many. Recovery always waits it out; going down can be given its
               own wait (`down_debounce_s`) for conditions that are themselves
               instantaneous, like a health check that times out.
  cooldown     After a down alert, another *down* alert for the same incident
               waits `cooldown_s`, so a connection that recovers and fails
               again every few minutes does not text on every failure.
  ceiling      No more than `max_per_window` alerts (down and recovery
               together) for one incident in a rolling `window_s`. Hard: when
               it is reached, everything else is suppressed and logged.

An *incident* is a key — `(kind, scope)` — so one failing check cannot
silence another's, and a stale snapshot and a slow AI are counted apart.

State is in this process only. That is deliberate: it cannot fail open (there
is no database to be unreachable). The cost is that a restart forgets what it
had told people, so an outage that spans a restart is announced once more.

This module sends nothing and knows nothing about email or SMS. `observe()`
says what to do; the caller does it and reports back with `sent()`. Until it
does, an alert stays owed and is offered again (no sooner than `retry_s`), so
a transport that failed is retried but one that succeeded is never repeated.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Hashable

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"

ALERT = "alert"
RECOVERY = "recovery"


@dataclass
class Verdict:
    """What to do about one observation. `action` is ALERT, RECOVERY or None;
    `reason` says why nothing (or something) happened, for the log."""
    action: str | None
    reason: str
    outage_s: float = 0.0
    texted: bool = False        # RECOVERY only: did the down alert text someone


@dataclass
class Incident:
    state: str = HEALTHY
    flip_at: float | None = None        # first observation disagreeing with `state`
    down_at: float | None = None        # first bad observation of this outage
    alerted: bool = False               # a down alert reached someone this outage
    texted: bool = False                # ...and it included a text
    last_attempt: float | None = None
    last_down_alert: float | None = None
    dispatched: deque = field(default_factory=deque)    # times of sent alerts


class AlertGate:
    def __init__(self, *, debounce_s: float = 60, min_outage_s: float = 60,
                 cooldown_s: float = 300, window_s: float = 1800,
                 max_per_window: int = 3, retry_s: float = 60,
                 clock: Callable[[], float] = time.time):
        self.debounce_s = debounce_s
        self.min_outage_s = min_outage_s
        self.cooldown_s = cooldown_s
        self.window_s = window_s
        self.max_per_window = max_per_window
        self.retry_s = retry_s
        self._clock = clock
        self._incidents: dict[Hashable, Incident] = {}
        self._lock = threading.Lock()

    # ── observing ──────────────────────────────────────────────────────────

    def observe(self, key: Hashable, healthy: bool, *,
                down_debounce_s: float = 0) -> Verdict:
        with self._lock:
            now = self._clock()
            inc = self._incidents.get(key)
            if inc is None:
                if healthy:
                    return Verdict(None, "healthy")
                inc = self._incidents[key] = Incident()
            self._trim(inc, now)
            if healthy:
                return self._observe_healthy(inc, now)
            return self._observe_unhealthy(inc, now, down_debounce_s)

    def _observe_unhealthy(self, inc: Incident, now: float,
                           down_debounce_s: float) -> Verdict:
        if inc.state == HEALTHY:
            if inc.flip_at is None:
                inc.flip_at = now
            if now - inc.flip_at < down_debounce_s:
                return Verdict(None, "down, debouncing")
            inc.state, inc.down_at, inc.flip_at = UNHEALTHY, inc.flip_at, None
            inc.alerted = inc.texted = False
        else:
            # Bad again: any recovery that was being counted is cancelled.
            # This is the whole flap defence — a connection has to stay good
            # for `debounce_s` without a single bad reading to count as back.
            inc.flip_at = None
        return self._owed(inc, now)

    def _observe_healthy(self, inc: Incident, now: float) -> Verdict:
        if inc.state == HEALTHY:
            inc.flip_at = None          # a blip that ended before it counted
            return Verdict(None, "healthy")
        if inc.flip_at is None:
            inc.flip_at = now
        if now - inc.flip_at < self.debounce_s:
            return Verdict(None, "recovery, debouncing")
        outage = inc.flip_at - (inc.down_at if inc.down_at is not None else inc.flip_at)
        told, texted = inc.alerted, inc.texted
        inc.state, inc.flip_at, inc.down_at = HEALTHY, None, None
        inc.alerted = inc.texted = False
        if not told:
            return Verdict(None, "recovered; the outage was never announced", outage)
        if outage < self.min_outage_s:
            return Verdict(None, "recovered; outage shorter than the minimum", outage)
        if len(inc.dispatched) >= self.max_per_window:
            return Verdict(None, "recovery suppressed: alert ceiling reached", outage)
        inc.last_attempt = now
        return Verdict(RECOVERY, "recovered", outage, texted)

    def _owed(self, inc: Incident, now: float) -> Verdict:
        """The incident is unhealthy: is a down alert due?"""
        if inc.alerted:
            return Verdict(None, "still down; already alerted")
        if len(inc.dispatched) >= self.max_per_window:
            return Verdict(None, "alert suppressed: ceiling reached")
        if (inc.last_down_alert is not None
                and now - inc.last_down_alert < self.cooldown_s):
            return Verdict(None, "alert suppressed: cooldown")
        if inc.last_attempt is not None and now - inc.last_attempt < self.retry_s:
            return Verdict(None, "waiting to retry a failed send")
        inc.last_attempt = now
        return Verdict(ALERT, "went down")

    # ── reporting back ─────────────────────────────────────────────────────

    def sent(self, key: Hashable, action: str, *, texted: bool = False) -> None:
        """Record that `action` reached someone. Only then does it count
        against the ceiling and stop being owed."""
        with self._lock:
            inc = self._incidents.get(key)
            if inc is None:
                inc = self._incidents[key] = Incident()
            now = self._clock()
            inc.dispatched.append(now)
            if action == ALERT:
                inc.alerted, inc.texted, inc.last_down_alert = True, texted, now

    def forget(self, scope: Hashable) -> None:
        """Drop every incident whose key ends in `scope`.
        Silent: something that no longer needs watching is not "recovered"."""
        with self._lock:
            for key in [k for k in self._incidents
                        if isinstance(k, tuple) and k and k[-1] == scope]:
                del self._incidents[key]

    def state(self, key: Hashable) -> str:
        inc = self._incidents.get(key)
        return inc.state if inc else HEALTHY

    def sent_count(self, key: Hashable) -> int:
        """Alerts sent for this incident inside the rolling window."""
        with self._lock:
            inc = self._incidents.get(key)
            if inc is None:
                return 0
            self._trim(inc, self._clock())
            return len(inc.dispatched)

    def _trim(self, inc: Incident, now: float) -> None:
        while inc.dispatched and now - inc.dispatched[0] >= self.window_s:
            inc.dispatched.popleft()
