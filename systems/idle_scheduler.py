# -*- coding: utf-8 -*-
"""
Idle Scheduler - Natural-feeling idle behavior for the desktop pet.

Design philosophy:
    The pet should feel alive, not like a cheap digital toy.
    It stays NEUTRAL most of the time, occasionally performing
    a micro-action (2-6s) before returning to NEUTRAL.

Timing:
    - Idle tick interval: 15-40 seconds (randomized each cycle)
    - Action duration:     2-6 seconds
    - Between actions:     NEUTRAL expression

Inactivity progression (time since last user interaction):
    - 60s  → READING (看书中)
    - 180s → SLEEPY  (困了)
    - 600s → TIRED   (累趴了) → enters SLEEP state

Idle action pool (weighted random):
    High frequency:
        SLEEPY     weight=3.0
    Mid frequency:
        READING    weight=1.5
        GREETING   weight=1.2
        EATING     weight=1.0
        DRINKING   weight=1.0
    Low frequency (rare surprises):
        COOL       weight=0.6
        HAPPY      weight=0.3
        SHY        weight=0.3

Extensibility:
    - Add new actions by appending to IDLE_ACTION_POOL.
    - Adjust weights to tune frequency.
    - Modify INACTIVITY_THRESHOLDS for different pacing.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Idle action pool (weighted random)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdleAction:
    """Definition of a single idle action."""
    expression: str          # Expression key (matches asset filename)
    weight: float = 1.0      # Selection weight (higher = more frequent)
    duration_ms: int = 4000  # How long the action lasts
    category: str = "mid"    # high / mid / low (for debugging / analytics)


# Default idle action pool.
# Weights are relative: SLEEPY(3.0) is ~3x more likely than EATING(1.0).
# HAPPY/SHY are intentionally very low to create occasional "micro-emotions".
IDLE_ACTION_POOL: list[IdleAction] = [
    # High frequency
    IdleAction("SLEEPY",   weight=3.0, duration_ms=4000, category="high"),
    # Mid frequency - life actions
    IdleAction("READING",  weight=1.5, duration_ms=5000, category="mid"),
    IdleAction("GREETING", weight=1.2, duration_ms=3000, category="mid"),
    IdleAction("EATING",   weight=1.0, duration_ms=4500, category="mid"),
    IdleAction("DRINKING", weight=1.0, duration_ms=3500, category="mid"),
    # Low frequency - rare surprises (彩蛋)
    IdleAction("COOL",     weight=0.6, duration_ms=3500, category="low"),
    IdleAction("HAPPY",    weight=0.3, duration_ms=3000, category="low"),
    IdleAction("SHY",      weight=0.3, duration_ms=3000, category="low"),
]


# ---------------------------------------------------------------------------
# Inactivity progression thresholds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InactivityTrigger:
    """Expression to show after a period of inactivity."""
    expression: str
    after_seconds: float
    duration_ms: int = 5000


INACTIVITY_TRIGGERS: list[InactivityTrigger] = [
    InactivityTrigger("READING",  after_seconds=60,   duration_ms=6000),
    InactivityTrigger("SLEEPY",   after_seconds=180,  duration_ms=6000),
    InactivityTrigger("TIRED",    after_seconds=600,  duration_ms=8000),
]


# ---------------------------------------------------------------------------
# Idle Scheduler
# ---------------------------------------------------------------------------

@dataclass
class IdleScheduler:
    """Manages idle behavior with natural, non-repetitive timing.

    The scheduler has two layers:
    1. Periodic micro-actions: every 15-40s, a random action plays for 2-6s.
    2. Inactivity progression: after set thresholds, show specific expressions.

    Usage:
        scheduler = IdleScheduler()
        scheduler.start()

        # Call every ~1 second from the main loop:
        result = scheduler.tick(now=datetime.now(), time_since_interaction=45.0)
        if result:
            show_expression(result.expression)
    """

    # Timing ranges (seconds)
    idle_interval_min: float = 15.0
    idle_interval_max: float = 40.0
    action_duration_min: float = 2.0
    action_duration_max: float = 6.0

    # Internal state
    _action_pool: list[IdleAction] = field(default_factory=lambda: list(IDLE_ACTION_POOL))
    _inactivity_triggers: list[InactivityTrigger] = field(default_factory=lambda: list(INACTIVITY_TRIGGERS))
    _rng: random.Random = field(default_factory=random.Random)

    # Timers
    _next_idle_at: Optional[datetime] = None
    _action_end_at: Optional[datetime] = None
    _current_action: Optional[IdleAction] = None
    _last_expression: Optional[str] = None  # Avoid repeating same expression
    _started: bool = False

    # ── Public API ──────────────────────────────────────────────────────

    def start(self, now: Optional[datetime] = None) -> None:
        """Start the idle scheduler. Call once at app startup."""
        self._started = True
        self._schedule_next_idle(now or datetime.now())

    def stop(self) -> None:
        """Stop the idle scheduler."""
        self._started = False
        self._next_idle_at = None
        self._action_end_at = None
        self._current_action = None

    def reset(self, now: Optional[datetime] = None) -> None:
        """Reset the scheduler (e.g., after an interaction ends)."""
        self._current_action = None
        self._action_end_at = None
        self._schedule_next_idle(now or datetime.now())

    def tick(
        self,
        now: datetime,
        time_since_interaction: float = 0.0,
    ) -> Optional[IdleAction]:
        """Called periodically (e.g., every second) when pet is in IDLE state.

        Returns an IdleAction if an expression change is needed, None otherwise.

        Args:
            now: Current timestamp.
            time_since_interaction: Seconds since last user interaction.
        """
        if not self._started:
            return None

        # Layer 2: Check inactivity progression first (higher priority).
        inactivity_result = self._check_inactivity(time_since_interaction)
        if inactivity_result is not None:
            return inactivity_result

        # Layer 1: Check if current action has expired.
        if self._current_action is not None and self._action_end_at is not None:
            if now >= self._action_end_at:
                # Action ended, return to NEUTRAL.
                self._current_action = None
                self._action_end_at = None
                self._schedule_next_idle(now)
                return IdleAction("NEUTRAL", weight=0, duration_ms=0, category="baseline")
            # Still playing current action.
            return None

        # Layer 1: Check if it's time to trigger a new idle action.
        if self._next_idle_at is not None and now >= self._next_idle_at:
            return self._trigger_action(now)

        return None

    # ── Internal ────────────────────────────────────────────────────────

    def _schedule_next_idle(self, now: datetime) -> None:
        """Schedule the next idle action after a random interval."""
        interval = self._rng.uniform(self.idle_interval_min, self.idle_interval_max)
        from datetime import timedelta
        self._next_idle_at = now + timedelta(seconds=interval)

    def _trigger_action(self, now: datetime) -> IdleAction:
        """Pick and start a random idle action."""
        action = self._pick_action()
        duration = self._rng.uniform(self.action_duration_min, self.action_duration_max)
        from datetime import timedelta

        self._current_action = action
        self._action_end_at = now + timedelta(seconds=duration)
        self._next_idle_at = None  # Will be set when action ends.
        self._last_expression = action.expression
        return action

    def _pick_action(self) -> IdleAction:
        """Weighted random selection from the idle pool.

        Avoids picking the same expression twice in a row.
        """
        # Build pool, optionally excluding last expression.
        pool = self._action_pool
        if self._last_expression and len(pool) > 1:
            filtered = [a for a in pool if a.expression != self._last_expression]
            if filtered:
                pool = filtered

        total_weight = sum(a.weight for a in pool)
        if total_weight <= 0:
            return pool[0]

        pick = self._rng.uniform(0, total_weight)
        upto = 0.0
        for action in pool:
            upto += action.weight
            if pick <= upto:
                return action
        return pool[-1]

    def _check_inactivity(self, time_since_interaction: float) -> Optional[IdleAction]:
        """Check if any inactivity threshold has been crossed.

        Returns the highest-priority trigger that matches.
        Triggers are checked in order (longest inactivity = highest priority).
        """
        for trigger in reversed(self._inactivity_triggers):
            if time_since_interaction >= trigger.after_seconds:
                # Don't re-trigger the same inactivity action.
                if self._current_action and self._current_action.expression == trigger.expression:
                    return None
                # Create an IdleAction from the inactivity trigger.
                return IdleAction(
                    expression=trigger.expression,
                    weight=0,
                    duration_ms=trigger.duration_ms,
                    category="inactivity",
                )
        return None

    # ── Configuration ───────────────────────────────────────────────────

    def set_pool(self, actions: list[IdleAction]) -> None:
        """Replace the idle action pool (for runtime customization)."""
        self._action_pool = list(actions)

    def set_inactivity_triggers(self, triggers: list[InactivityTrigger]) -> None:
        """Replace inactivity triggers (for runtime customization)."""
        self._inactivity_triggers = list(triggers)

    def get_stats(self) -> dict:
        """Return scheduler stats for debugging."""
        return {
            "started": self._started,
            "current_action": self._current_action.expression if self._current_action else "NEUTRAL",
            "last_expression": self._last_expression,
        }
