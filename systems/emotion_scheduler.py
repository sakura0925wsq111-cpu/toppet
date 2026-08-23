# -*- coding: utf-8 -*-
"""Desktop emotion scheduling around the shared semantic resolver.

``PetEmotionScheduler`` retains its public API, while semantic decisions are
delegated to :mod:`systems.emotion_resolver`.  This wrapper owns desktop-only
concerns: UI phases, idle scheduling, expression duration, and calls into the
``ExpressionManager``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .emotion_resolver import (
    AMBIENT_IDLE_EMOTIONS,
    EMOTION_ALIASES,
    EMOTION_ASSETS,
    EMOTION_GROUPS,
    EMOTION_PROMPT,
    EMOTION_RANKS,
    NIGHT_EMOTIONS,
    VALID_EMOTIONS,
    EmotionCandidate,
    EmotionResolution,
    EmotionResolver,
)
from .expression_manager import ExpressionManager
from .idle_scheduler import IdleAction
from .state_machine import PetState


@dataclass(frozen=True)
class EmotionProfile:
    key: str
    category: str
    priority: int
    duration_ms: int
    cooldown_ms: int
    weight: float = 1.0


_PLAYBACK_PROFILES: dict[str, tuple[str, int]] = {
    "NEUTRAL": ("HIGH", 0),
    "LISTENING": ("HIGH", 900),
    "THINKING": ("HIGH", 0),
    "WORKING": ("HIGH", 5200),
    "GOOD_NIGHT": ("HIGH", 6500),
    "TIRED": ("HIGH", 5200),
    "HAPPY": ("MEDIUM", 3600),
    "SHY": ("MEDIUM", 4300),
    "SURPRISED": ("MEDIUM", 3300),
    "SAD": ("MEDIUM", 5200),
    "LOVE": ("MEDIUM", 4300),
    "HEART": ("MEDIUM", 4800),
    "ANGRY": ("LOW", 2800),
    "COOL": ("LOW", 3800),
    "EATING": ("LOW", 4500),
    "DRINKING": ("LOW", 3800),
    "CELEBRATE": ("LOW", 4600),
    "SLEEPY": ("NIGHT", 5600),
    "READING": ("AMBIENT", 6200),
    "GREETING": ("AMBIENT", 3600),
}

EMOTION_PROFILES = {
    emotion: EmotionProfile(
        key=emotion,
        category=category,
        priority=EMOTION_RANKS[emotion].priority,
        duration_ms=duration_ms,
        cooldown_ms=EMOTION_RANKS[emotion].cooldown_ms,
        weight=EMOTION_RANKS[emotion].weight,
    )
    for emotion, (category, duration_ms) in _PLAYBACK_PROFILES.items()
}


@dataclass(frozen=True)
class EmotionDecision:
    emotion: str
    duration_ms: int
    reason: str
    category: str


class PetEmotionScheduler:
    """Coordinate desktop state and playback around an ``EmotionResolver``."""

    idle_after_seconds = 180
    praise_window_seconds = EmotionResolver.praise_window_seconds

    # Backward-compatible access to the established pattern catalog.
    praise_patterns = EmotionResolver.praise_patterns
    tired_patterns = EmotionResolver.tired_patterns
    sad_patterns = EmotionResolver.sad_patterns
    love_patterns = EmotionResolver.love_patterns
    surprise_patterns = EmotionResolver.surprise_patterns
    anger_patterns = EmotionResolver.anger_patterns
    food_patterns = EmotionResolver.food_patterns
    drink_patterns = EmotionResolver.drink_patterns
    celebrate_patterns = EmotionResolver.celebrate_patterns
    work_patterns = EmotionResolver.work_patterns
    reading_patterns = EmotionResolver.reading_patterns
    bedtime_patterns = EmotionResolver.bedtime_patterns
    cool_patterns = EmotionResolver.cool_patterns

    def __init__(
        self,
        rng: random.Random | None = None,
        resolver: EmotionResolver | None = None,
    ) -> None:
        self.resolver = resolver or EmotionResolver(rng=rng)
        self.rng = self.resolver.rng
        self._expr_mgr = ExpressionManager()

        self.phase = "IDLE"
        self.current_emotion = "NEUTRAL"
        self.last_interaction_at: datetime | None = None
        # Preserve the legacy inspection surface without duplicating state.
        self.last_emotion_at = self.resolver.last_emotion_at

    @property
    def praise_streak(self) -> int:
        return self.resolver.praise_streak

    @praise_streak.setter
    def praise_streak(self, value: int) -> None:
        self.resolver.praise_streak = value

    @property
    def last_praise_at(self) -> datetime | None:
        return self.resolver.last_praise_at

    @last_praise_at.setter
    def last_praise_at(self, value: datetime | None) -> None:
        self.resolver.last_praise_at = value

    def start_idle_clock(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self.last_interaction_at = now
        self._expr_mgr.last_interaction_at = now
        self._expr_mgr.idle_scheduler.start(now)

    @staticmethod
    def normalize_emotion(raw_emotion: str | None) -> str:
        return EmotionResolver.normalize_emotion(raw_emotion)

    @staticmethod
    def is_late_night(now: datetime) -> bool:
        return EmotionResolver.is_late_night(now)

    def on_user_input(
        self, user_text: str, now: datetime | None = None
    ) -> EmotionDecision:
        """Record the turn and force the state-machine-owned LISTENING state."""
        now = now or datetime.now()
        self.last_interaction_at = now
        self.resolver.observe_user_input(user_text, now)
        self.phase = "LISTENING"
        self._expr_mgr.on_user_input()
        return self._mark("LISTENING", now, "用户输入，先认真听")

    def on_ai_start(self, now: datetime | None = None) -> EmotionDecision:
        """Force the state-machine-owned THINKING state."""
        now = now or datetime.now()
        self.phase = "THINKING"
        self._expr_mgr.on_ai_start()
        return self._mark("THINKING", now, "AI 正在生成回复")

    def on_reply(
        self,
        user_text: str,
        assistant_text: str,
        model_emotion: str | None,
        now: datetime | None = None,
    ) -> EmotionDecision:
        now = now or datetime.now()
        self.last_interaction_at = now
        self.phase = "RESPONDING"

        resolution = self.resolver.resolve(
            user_text,
            assistant_text,
            model_emotion,
            now,
        )
        if resolution.emotion != "NEUTRAL":
            self._expr_mgr.on_ai_response(
                user_text,
                assistant_text,
                model_emotion or "NEUTRAL",
            )
        else:
            self._expr_mgr.on_ai_response(user_text, assistant_text, "NEUTRAL")
        return self._decision(resolution)

    def on_error(self, now: datetime | None = None) -> EmotionDecision:
        now = now or datetime.now()
        self.phase = "RESPONDING"
        self._expr_mgr.on_error()
        resolution = self.resolver.choose(
            (
                EmotionCandidate("ANGRY", "处理失败，有点不爽", 45),
                EmotionCandidate("TIRED", "处理失败，累趴一下", 20),
            ),
            now,
        )
        if resolution is None:
            return self.default_decision(now, "处理失败但表情在冷却")
        return self._decision(resolution)

    def maybe_idle(self, now: datetime | None = None) -> EmotionDecision | None:
        """Run desktop-only idle scheduling when the state machine permits it."""
        now = now or datetime.now()
        if self.phase in {"LISTENING", "THINKING"}:
            return None
        if not self._expr_mgr.is_idle():
            return None

        result = self._expr_mgr.on_idle_tick(now)
        if result is None:
            return None
        if result == "NEUTRAL":
            self.last_interaction_at = now
            return self.default_decision(now, "idle动作结束，回到待机")

        self.last_interaction_at = now
        return self._mark(result, now, f"idle调度器触发: {result}")

    def default_decision(
        self,
        now: datetime | None = None,
        reason: str = "回到待机",
        skip_night: bool = False,
    ) -> EmotionDecision:
        now = now or datetime.now()
        self.phase = "IDLE"
        return self._decision(
            self.resolver.resolve_default(
                now,
                reason,
                skip_night=skip_night,
            )
        )

    # Backward-compatible internal helpers now delegated to the resolver.
    def _semantic_candidates(
        self,
        user_text: str,
        assistant_text: str,
        model_emotion: str | None,
        now: datetime,
    ) -> list[EmotionCandidate]:
        return self.resolver.semantic_candidates(
            user_text,
            assistant_text,
            model_emotion,
            now,
        )

    def _choose(
        self,
        candidates: Iterable[EmotionCandidate],
        now: datetime,
    ) -> EmotionDecision | None:
        resolution = self.resolver.choose(candidates, now)
        return self._decision(resolution) if resolution is not None else None

    def _mark(self, emotion: str, now: datetime, reason: str) -> EmotionDecision:
        return self._decision(self.resolver.mark(emotion, now, reason))

    def _decision(self, resolution: EmotionResolution) -> EmotionDecision:
        profile = EMOTION_PROFILES.get(
            resolution.emotion,
            EMOTION_PROFILES["NEUTRAL"],
        )
        self.current_emotion = profile.key
        return EmotionDecision(
            emotion=profile.key,
            duration_ms=profile.duration_ms,
            reason=resolution.reason,
            category=profile.category,
        )

    def _is_available(self, emotion: str, now: datetime) -> bool:
        return self.resolver.is_available(emotion, now)

    def _update_praise_streak(self, user_text: str, now: datetime) -> None:
        self.resolver.observe_user_input(user_text, now)

    @staticmethod
    def _compile(patterns: Iterable[str] | str):
        return EmotionResolver._compile(patterns)

    @staticmethod
    def _matches(text: str, pattern) -> bool:
        return EmotionResolver._matches(text, pattern)


__all__ = [
    "AMBIENT_IDLE_EMOTIONS",
    "EMOTION_ALIASES",
    "EMOTION_ASSETS",
    "EMOTION_GROUPS",
    "EMOTION_PROFILES",
    "EMOTION_PROMPT",
    "NIGHT_EMOTIONS",
    "VALID_EMOTIONS",
    "EmotionCandidate",
    "EmotionDecision",
    "EmotionProfile",
    "PetEmotionScheduler",
    "IdleAction",
    "PetState",
]
