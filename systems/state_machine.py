# -*- coding: utf-8 -*-
"""
Pet State Machine - Core state management for the desktop pet.

States (priority order): EMOTION > THINKING > TALKING > IDLE > SLEEP

- IDLE:     Default state when no interaction is happening. Idle scheduler runs here.
- TALKING:  User is typing / pet is listening. Shows LISTENING expression.
- THINKING: AI is generating a response. Shows THINKING expression.
- EMOTION:  Active emotional expression triggered by interaction (HAPPY, ANGRY, etc.)
- SLEEP:    Deep idle after very long inactivity. Shows TIRED expression.

Transition rules:
- Higher priority states can interrupt lower ones.
- IDLE can only be entered when no higher-priority state is active.
- SLEEP can only be entered from IDLE after prolonged inactivity.
- TALKING is forced when user starts typing (from any state).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional


class PetState(Enum):
    """All possible states the pet can be in."""
    IDLE = auto()
    TALKING = auto()
    THINKING = auto()
    EMOTION = auto()
    SLEEP = auto()


# Priority values: higher number = higher priority.
# States with higher priority can override lower-priority states.
STATE_PRIORITY: dict[PetState, int] = {
    PetState.EMOTION:  100,
    PetState.THINKING: 90,
    PetState.TALKING:  80,
    PetState.IDLE:     10,
    PetState.SLEEP:     5,
}


def can_transition(current: PetState, target: PetState) -> bool:
    """Check if transitioning from `current` to `target` is allowed.

    Transitions are always allowed. Priority enforcement is handled by
    the caller (ExpressionManager / PetEmotionScheduler) rather than
    the state machine itself, which serves as a record-keeper.

    Use `STATE_PRIORITY` to inform priority-based decisions in calling code.
    """
    return True


def can_interrupt(current: PetState, target: PetState) -> bool:
    """Legacy alias for can_transition. Always returns True."""
    return True


@dataclass
class StateTransition:
    """Record of a state change."""
    from_state: PetState
    to_state: PetState
    timestamp: datetime
    reason: str = ""


@dataclass
class StateMachine:
    """Finite state machine for the desktop pet.

    Manages state transitions with priority-based interruption rules.
    Maintains a transition history for debugging and analytics.

    Usage:
        sm = StateMachine()
        sm.transition(PetState.TALKING, "user_started_typing")
        assert sm.current == PetState.TALKING
        sm.transition(PetState.IDLE, "conversation_ended")  # allowed (lower priority)
    """

    current: PetState = PetState.IDLE
    previous: Optional[PetState] = None
    entered_at: datetime = field(default_factory=datetime.now)
    history: list[StateTransition] = field(default_factory=list)
    max_history: int = 64

    def transition(self, target: PetState, reason: str = "") -> bool:
        """Attempt to transition to `target` state.

        Returns True if transition succeeded, False if blocked by priority rules.
        """
        if self.current == target:
            return True  # Already in target state, nothing to do.

        if not can_transition(self.current, target):
            return False

        # Record the transition.
        transition = StateTransition(
            from_state=self.current,
            to_state=target,
            timestamp=datetime.now(),
            reason=reason,
        )
        self.history.append(transition)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

        # Update state.
        self.previous = self.current
        self.current = target
        self.entered_at = transition.timestamp
        return True

    def force(self, target: PetState, reason: str = "") -> None:
        """Force a transition regardless of priority rules.

        Use sparingly - only for system-level resets or user-initiated
        actions that must always take effect (e.g., user starts typing).
        """
        if self.current == target:
            return

        transition = StateTransition(
            from_state=self.current,
            to_state=target,
            timestamp=datetime.now(),
            reason=f"[FORCED] {reason}",
        )
        self.history.append(transition)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

        self.previous = self.current
        self.current = target
        self.entered_at = transition.timestamp

    def is_idle(self) -> bool:
        """Check if the pet is in an idle-related state (IDLE or SLEEP)."""
        return self.current in (PetState.IDLE, PetState.SLEEP)

    def is_interacting(self) -> bool:
        """Check if the pet is currently engaged in interaction."""
        return self.current in (PetState.TALKING, PetState.THINKING, PetState.EMOTION)

    def time_in_state(self) -> float:
        """Return seconds spent in the current state."""
        return (datetime.now() - self.entered_at).total_seconds()

    def reset(self) -> None:
        """Reset the state machine to IDLE."""
        self.force(PetState.IDLE, "system_reset")

    # ------------------------------------------------------------------
    # Convenience methods for common transitions
    # ------------------------------------------------------------------

    def on_user_input(self) -> bool:
        """User starts typing - always transition to TALKING."""
        self.force(PetState.TALKING, "user_started_typing")
        return True

    def on_ai_start(self) -> bool:
        """AI begins generating - transition to THINKING if currently TALKING."""
        return self.transition(PetState.THINKING, "ai_started_generating")

    def on_emotion_trigger(self, emotion_name: str) -> bool:
        """An emotion was triggered by interaction."""
        return self.transition(PetState.EMOTION, f"emotion:{emotion_name}")

    def on_interaction_end(self) -> bool:
        """Interaction ended, return to IDLE."""
        return self.transition(PetState.IDLE, "interaction_ended")

    def on_enter_sleep(self) -> bool:
        """Enter deep sleep after prolonged inactivity."""
        return self.transition(PetState.SLEEP, "prolonged_inactivity")

    def on_wake_from_sleep(self) -> bool:
        """Wake up from sleep when user interacts."""
        self.force(PetState.TALKING, "wake_from_sleep")
        return True
