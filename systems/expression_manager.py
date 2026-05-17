# -*- coding: utf-8 -*-
"""
Expression Manager - High-level coordinator for the pet's expression system.

This is the main interface between the UI (laoshaozi.py) and the state machine +
idle scheduler. It handles:

1. State transitions based on user/AI interaction.
2. Idle behavior scheduling with weighted random actions.
3. Inactivity progression (reading → sleepy → tired).
4. Cooldown management to prevent expression spam.

The main app should create one ExpressionManager instance and call its methods
at the appropriate lifecycle points.

Usage:
    mgr = ExpressionManager(rng_seed=42)

    # User starts typing
    expression = mgr.on_user_input()  # → "LISTENING"

    # AI starts generating
    expression = mgr.on_ai_start()    # → "THINKING"

    # AI response complete, with optional emotion detection
    expression = mgr.on_ai_response(
        user_text="你好可爱",
        ai_text="谢谢你！",
        model_emotion="HAPPY",
        emotion_matcher=my_matcher,  # Optional: keyword-based emotion detection
    )

    # Periodic idle tick (call every ~1s when in IDLE)
    expression = mgr.on_idle_tick()  # → "NEUTRAL" or "READING" or "SLEEPY" etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .state_machine import PetState, StateMachine
from .idle_scheduler import IdleAction, IdleScheduler


# Type alias for an external emotion matcher function.
# Takes (user_text, ai_text, model_emotion) and returns an emotion key or None.
EmotionMatcher = Callable[[str, str, str], Optional[str]]


@dataclass
class ExpressionManager:
    """Central coordinator for pet expressions and state.

    Coordinates between:
    - StateMachine: enforces state priority and transitions.
    - IdleScheduler: manages natural-feeling idle behavior.
    - EmotionMatcher (optional): keyword-based emotion detection.
    """

    state_machine: StateMachine = field(default_factory=StateMachine)
    idle_scheduler: IdleScheduler = field(default_factory=IdleScheduler)

    # Tracking
    last_interaction_at: datetime = field(default_factory=datetime.now)
    current_expression: str = "NEUTRAL"
    emotion_matcher: Optional[EmotionMatcher] = None

    def __post_init__(self) -> None:
        self.idle_scheduler.start()

    # ── Interaction lifecycle ───────────────────────────────────────────

    def on_user_input(self) -> str:
        """Called when the user starts typing.

        Forces TALKING state and LISTENING expression regardless of current state.
        """
        self.last_interaction_at = datetime.now()
        self.state_machine.on_user_input()
        self.current_expression = "LISTENING"
        return self.current_expression

    def on_ai_start(self) -> str:
        """Called when the AI begins generating a response.

        Transitions to THINKING state if currently TALKING.
        """
        self.state_machine.on_ai_start()
        if self.state_machine.current == PetState.THINKING:
            self.current_expression = "THINKING"
        return self.current_expression

    def on_ai_response(
        self,
        user_text: str,
        ai_text: str,
        model_emotion: str = "NEUTRAL",
    ) -> str:
        """Called when the AI response is complete.

        Determines the appropriate expression based on:
        1. Emotion matcher (keyword-based detection)
        2. Model-suggested emotion
        3. Falls back to NEUTRAL → IDLE
        """
        # Try emotion matcher first if available.
        matched_emotion: Optional[str] = None
        if self.emotion_matcher is not None:
            matched_emotion = self.emotion_matcher(user_text, ai_text, model_emotion)

        if matched_emotion and matched_emotion != "NEUTRAL":
            # An emotion was triggered by the interaction.
            self.state_machine.on_emotion_trigger(matched_emotion)
            self.current_expression = matched_emotion
        else:
            # No special emotion - return to IDLE.
            self.state_machine.on_interaction_end()
            self.current_expression = "NEUTRAL"
            self.idle_scheduler.reset()

        self.last_interaction_at = datetime.now()
        return self.current_expression

    def on_error(self) -> str:
        """Called when AI processing fails. Returns to IDLE."""
        self.state_machine.on_interaction_end()
        self.current_expression = "NEUTRAL"
        self.idle_scheduler.reset()
        return self.current_expression

    # ── Idle tick ───────────────────────────────────────────────────────

    def on_idle_tick(self, now: Optional[datetime] = None) -> Optional[str]:
        """Called periodically (~1s) when the pet might be idle.

        Returns a new expression if one is needed, or None if nothing changed.
        Only processes idle logic when the state machine is in IDLE/SLEEP.
        """
        if now is None:
            now = datetime.now()

        if not self.state_machine.is_idle():
            return None

        time_since = (now - self.last_interaction_at).total_seconds()

        result = self.idle_scheduler.tick(now, time_since_interaction=time_since)
        if result is None:
            return None

        # Check if inactivity triggered SLEEP state.
        if result.expression == "TIRED":
            self.state_machine.on_enter_sleep()

        # If returning to NEUTRAL, ensure we're in IDLE (not SLEEP).
        if result.expression == "NEUTRAL" and self.state_machine.current == PetState.SLEEP:
            self.state_machine.on_interaction_end()

        self.current_expression = result.expression
        return result.expression

    # ── State queries ───────────────────────────────────────────────────

    def get_state(self) -> PetState:
        """Return the current state machine state."""
        return self.state_machine.current

    def get_expression(self) -> str:
        """Return the current expression."""
        return self.current_expression

    def is_idle(self) -> bool:
        """Check if the pet is idle (can safely run idle scheduler)."""
        return self.state_machine.is_idle()

    def time_since_interaction(self) -> float:
        """Return seconds since last user interaction."""
        return (datetime.now() - self.last_interaction_at).total_seconds()

    # ── Configuration ───────────────────────────────────────────────────

    def set_emotion_matcher(self, matcher: EmotionMatcher) -> None:
        """Set the emotion matcher function for keyword-based emotion detection."""
        self.emotion_matcher = matcher

    def set_idle_pool(self, actions: list[IdleAction]) -> None:
        """Customize the idle action pool."""
        self.idle_scheduler.set_pool(actions)

    def update_idle_timing(
        self,
        interval_min: Optional[float] = None,
        interval_max: Optional[float] = None,
        duration_min: Optional[float] = None,
        duration_max: Optional[float] = None,
    ) -> None:
        """Adjust idle timing parameters at runtime."""
        if interval_min is not None:
            self.idle_scheduler.idle_interval_min = interval_min
        if interval_max is not None:
            self.idle_scheduler.idle_interval_max = interval_max
        if duration_min is not None:
            self.idle_scheduler.action_duration_min = duration_min
        if duration_max is not None:
            self.idle_scheduler.action_duration_max = duration_max

    def reset(self) -> None:
        """Full reset - return to IDLE with fresh scheduler."""
        self.state_machine.reset()
        self.idle_scheduler.reset()
        self.current_expression = "NEUTRAL"
        self.last_interaction_at = datetime.now()
