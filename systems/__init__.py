# -*- coding: utf-8 -*-
"""Systems package - Core state machine and scheduling for the desktop pet."""

from .state_machine import PetState, StateMachine, can_transition, can_interrupt, STATE_PRIORITY
from .idle_scheduler import IdleAction, IdleScheduler, IDLE_ACTION_POOL, INACTIVITY_TRIGGERS
from .expression_manager import ExpressionManager, EmotionMatcher

__all__ = [
    "PetState",
    "StateMachine",
    "can_transition",
    "can_interrupt",
    "STATE_PRIORITY",
    "IdleAction",
    "IdleScheduler",
    "IDLE_ACTION_POOL",
    "INACTIVITY_TRIGGERS",
    "ExpressionManager",
    "EmotionMatcher",
]
