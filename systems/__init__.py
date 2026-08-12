# -*- coding: utf-8 -*-
"""Systems package - Core state machine and scheduling for the desktop pet."""

from .state_machine import PetState, StateMachine, can_transition, can_interrupt, STATE_PRIORITY
from .idle_scheduler import IdleAction, IdleScheduler, IDLE_ACTION_POOL, INACTIVITY_TRIGGERS
from .expression_manager import ExpressionManager, EmotionMatcher
from .character_manager import CharacterManager, CHARACTER_DISPLAY_NAMES, VALID_CHARACTERS, DEFAULT_CHARACTER
from .tts_service_manager import TTSServiceManager
from .voxcpm_service_manager import VoxCPMServiceManager

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
    "CharacterManager",
    "CHARACTER_DISPLAY_NAMES",
    "VALID_CHARACTERS",
    "DEFAULT_CHARACTER",
    "TTSServiceManager",
    "VoxCPMServiceManager",
]
