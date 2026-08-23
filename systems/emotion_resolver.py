# -*- coding: utf-8 -*-
"""Shared, UI-independent semantic emotion resolution.

The desktop scheduler and the hardware host service both compose an
``EmotionResolver``.  This module deliberately has no PyQt dependency and
contains the complete semantic decision policy: the emotion catalog, aliases,
keyword matching, candidate ranking, model-emotion fusion, praise streaks,
cooldowns, and late-night candidates.

``LISTENING`` and ``THINKING`` remain in the catalog because they are valid
expression assets, but they are owned by interaction state machines and are
never returned by semantic resolution.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


EMOTION_ASSETS = {
    "NEUTRAL": "待机、普通、日常回复",
    "HAPPY": "开心、轻松、夸好",
    "SHY": "害羞、不好意思、被夸到",
    "SLEEPY": "困了、想睡、疲惫但还能聊",
    "THINKING": "思考、疑惑、分析问题",
    "ANGRY": "生气、不满、护短、吐槽",
    "SURPRISED": "惊讶、震惊、突然发现",
    "SAD": "难过、委屈、需要安慰",
    "LOVE": "喜欢、心动、撒娇",
    "COOL": "超酷、自信、装酷",
    "GREETING": "打招呼、欢迎、召唤用户",
    "WORKING": "工作中、认真处理任务",
    "READING": "学习、看书、查资料",
    "EATING": "吃东西、美食、热量话题",
    "DRINKING": "喝水、提醒补水",
    "GOOD_NIGHT": "晚安、睡前陪伴",
    "LISTENING": "听你说、认真倾听",
    "HEART": "比心、鼓励、感谢",
    "CELEBRATE": "庆祝、完成任务、好消息",
    "TIRED": "累趴了、压力大、崩溃但可爱",
}

EMOTION_GROUPS = {
    "HIGH": ("NEUTRAL", "THINKING", "LISTENING", "WORKING", "GOOD_NIGHT", "TIRED"),
    "MEDIUM": ("HAPPY", "SHY", "SURPRISED", "SAD", "LOVE", "HEART"),
    "LOW": ("ANGRY", "COOL", "EATING", "DRINKING", "CELEBRATE"),
}

AMBIENT_IDLE_EMOTIONS = ("READING", "WORKING", "EATING", "DRINKING")
NIGHT_EMOTIONS = ("SLEEPY", "GOOD_NIGHT")
STATE_CONTROLLED_EMOTIONS = frozenset({"LISTENING", "THINKING"})
VALID_EMOTIONS = set(EMOTION_ASSETS)
SEMANTIC_EMOTIONS = frozenset(VALID_EMOTIONS - STATE_CONTROLLED_EMOTIONS)

EMOTION_ALIASES = {
    "待机": "NEUTRAL",
    "开心": "HAPPY",
    "害羞": "SHY",
    "困了": "SLEEPY",
    "思考": "THINKING",
    "生气": "ANGRY",
    "惊讶": "SURPRISED",
    "难过": "SAD",
    "喜欢": "LOVE",
    "超酷": "COOL",
    "打招呼": "GREETING",
    "工作中": "WORKING",
    "看书中": "READING",
    "吃东西": "EATING",
    "喝水": "DRINKING",
    "晚安": "GOOD_NIGHT",
    "听你说": "LISTENING",
    "给你比心": "HEART",
    "比心": "HEART",
    "庆祝": "CELEBRATE",
    "累趴了": "TIRED",
    "超开心": "HAPPY",
}

EMOTION_PROMPT = "\n".join(f"- {key}: {hint}" for key, hint in EMOTION_ASSETS.items())


@dataclass(frozen=True)
class EmotionRank:
    """Decision metadata independent of UI playback duration."""

    priority: int
    cooldown_ms: int
    weight: float = 1.0


EMOTION_RANKS = {
    "NEUTRAL": EmotionRank(10, 0, 8.0),
    "LISTENING": EmotionRank(95, 1500, 6.0),
    "THINKING": EmotionRank(100, 1200, 6.0),
    "WORKING": EmotionRank(45, 45000, 3.0),
    "GOOD_NIGHT": EmotionRank(80, 240000, 2.0),
    "TIRED": EmotionRank(78, 90000, 2.2),
    "HAPPY": EmotionRank(58, 8000, 3.2),
    "SHY": EmotionRank(68, 12000, 2.2),
    "SURPRISED": EmotionRank(70, 12000, 2.0),
    "SAD": EmotionRank(74, 15000, 2.0),
    "LOVE": EmotionRank(72, 15000, 1.8),
    "HEART": EmotionRank(82, 15000, 1.7),
    "ANGRY": EmotionRank(55, 180000, 1.0),
    "COOL": EmotionRank(48, 180000, 1.0),
    "EATING": EmotionRank(42, 240000, 1.1),
    "DRINKING": EmotionRank(42, 180000, 1.2),
    "CELEBRATE": EmotionRank(70, 120000, 1.5),
    "SLEEPY": EmotionRank(76, 140000, 2.0),
    "READING": EmotionRank(38, 180000, 2.4),
    "GREETING": EmotionRank(50, 60000, 1.4),
}


@dataclass(frozen=True)
class EmotionCandidate:
    emotion: str
    reason: str
    priority_bonus: int = 0
    weight_multiplier: float = 1.0


@dataclass(frozen=True)
class EmotionResolution:
    emotion: str
    reason: str


class EmotionResolver:
    """Resolve reply semantics to one expression without UI dependencies."""

    praise_window_seconds = 180

    praise_patterns = (
        r"可爱|好可爱|萌|乖|喜欢|爱你|亲亲|抱抱|贴贴",
        r"厉害|太强|真棒|好棒|牛|聪明|靠谱|优秀|帮|帅气",
        r"谢谢|感谢|辛苦|你真好|夸赞|表扬",
    )
    tired_patterns = (
        r"好累|累死|累了|困|困死|熬夜|睡不着",
        r"崩溃|压力|撑不住|烦死|emo",
    )
    sad_patterns = (r"难过|委屈|伤心|想哭|不开心|低落|焦虑|害怕",)
    love_patterns = (r"喜欢|爱你|想你|亲亲|抱抱|贴贴|心动",)
    surprise_patterns = (r"惊讶|震惊|吓我|没想到|真的假的|不会吧|啊？|\?|wow|哇",)
    anger_patterns = (r"生气|气死|火大|烦死|讨厌|不爽|离谱|过分",)
    food_patterns = (r"吃|饭|饿|零食|奶茶|咖啡|热量|卡路里|外卖|宵夜",)
    drink_patterns = (r"喝水|口渴|补水|水杯|饮料",)
    celebrate_patterns = (r"完成|成功|通过|搞定|赢了|太好|庆祝|好消息|顺利",)
    work_patterns = (r"代码|项目|修|改|写|实现|测试|bug|报错|工作|任务|帮我",)
    reading_patterns = (r"学习|看书|资料|论文|复习|知识点|查一下",)
    bedtime_patterns = (r"晚安|睡觉|睡了|梦里见|该睡|休息",)
    cool_patterns = (r"酷|帅|装一下|墨镜|帅气",)

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.praise_streak = 0
        self.last_praise_at: datetime | None = None
        self.last_emotion_at: dict[str, datetime] = {}

        self._re_tired = self._compile(self.tired_patterns)
        self._re_sad = self._compile(self.sad_patterns)
        self._re_love = self._compile(self.love_patterns)
        self._re_praise = self._compile(self.praise_patterns)
        self._re_surprise = self._compile(self.surprise_patterns)
        self._re_anger = self._compile(self.anger_patterns)
        self._re_food = self._compile(self.food_patterns)
        self._re_drink = self._compile(self.drink_patterns)
        self._re_celebrate = self._compile(self.celebrate_patterns)
        self._re_work = self._compile(self.work_patterns)
        self._re_reading = self._compile(self.reading_patterns)
        self._re_bedtime = self._compile(self.bedtime_patterns)
        self._re_cool = self._compile(self.cool_patterns)

    @staticmethod
    def normalize_emotion(raw_emotion: str | None) -> str:
        raw = str(raw_emotion or "NEUTRAL").strip()
        return EMOTION_ALIASES.get(raw, re.sub(r"[\s-]+", "_", raw.upper()))

    @staticmethod
    def is_late_night(now: datetime) -> bool:
        return now.hour >= 23 or now.hour < 6

    def observe_user_input(
        self, user_text: str, now: datetime | None = None
    ) -> None:
        """Update per-conversation praise state for a new user turn."""
        now = now or datetime.now()
        if not self._matches(user_text, self._re_praise):
            return
        if self.last_praise_at is None:
            self.praise_streak = 1
        elif (now - self.last_praise_at).total_seconds() <= self.praise_window_seconds:
            self.praise_streak += 1
        else:
            self.praise_streak = 1
        self.last_praise_at = now

    def resolve(
        self,
        user_text: str,
        assistant_text: str,
        model_emotion: str | None,
        now: datetime | None = None,
        *,
        fallback_reason: str = "回复完成，无强触发表情",
    ) -> EmotionResolution:
        """Resolve a completed reply, falling back to ``NEUTRAL``."""
        now = now or datetime.now()
        decision = self.choose(
            self.semantic_candidates(user_text, assistant_text, model_emotion, now),
            now,
        )
        if decision is not None:
            return decision
        return self.mark("NEUTRAL", now, fallback_reason)

    def resolve_default(
        self,
        now: datetime | None = None,
        reason: str = "回到待机",
        *,
        skip_night: bool = False,
    ) -> EmotionResolution:
        """Resolve the default expression, including the late-night policy."""
        now = now or datetime.now()
        if self.is_late_night(now) and not skip_night:
            decision = self.choose(
                (
                    EmotionCandidate("SLEEPY", "深夜待机，更容易困", 8, 1.6),
                    EmotionCandidate("GOOD_NIGHT", "深夜待机，更容易晚安", 8, 1.2),
                    EmotionCandidate("NEUTRAL", reason, 0, 3.0),
                ),
                now,
            )
            if decision is not None:
                return decision
        return self.mark("NEUTRAL", now, reason)

    def semantic_candidates(
        self,
        user_text: str,
        assistant_text: str,
        model_emotion: str | None,
        now: datetime,
    ) -> list[EmotionCandidate]:
        """Build candidates from text, conversation state, time, and the model."""
        text = f"{user_text}\n{assistant_text}"
        candidates: list[EmotionCandidate] = []

        if self._matches(user_text, self._re_bedtime):
            candidates.extend((
                EmotionCandidate("GOOD_NIGHT", "用户提到晚安/睡觉", 58, 1.6),
                EmotionCandidate("SLEEPY", "用户提到困意", 45, 1.4),
            ))
        if self._matches(text, self._re_tired):
            candidates.extend((
                EmotionCandidate("TIRED", "用户显得很累", 48, 1.5),
                EmotionCandidate("SAD", "用户需要安慰", 28, 1.2),
            ))
        if self._matches(text, self._re_sad):
            candidates.extend((
                EmotionCandidate("SAD", "用户情绪低落", 45, 1.4),
                EmotionCandidate("HEART", "给用户一点鼓励", 28, 1.0),
            ))
        if self._matches(text, self._re_love):
            candidates.extend((
                EmotionCandidate("LOVE", "亲密/喜欢语义", 46, 1.4),
                EmotionCandidate("HEART", "亲密时比心", 35, 1.0),
                EmotionCandidate("SHY", "被喜欢时害羞", 24, 1.0),
            ))
        if self.praise_streak >= 3:
            candidates.extend((
                EmotionCandidate("HEART", "连续夸赞触发比心", 70, 1.8),
                EmotionCandidate("SHY", "连续夸赞有点害羞", 55, 1.5),
                EmotionCandidate("HAPPY", "连续夸赞超开心", 42, 1.4),
            ))
        elif self.praise_streak >= 2:
            candidates.extend((
                EmotionCandidate("SHY", "连续夸赞，害羞一下", 42, 1.4),
                EmotionCandidate("HAPPY", "连续夸赞，开心一下", 28, 1.2),
            ))
        elif self._matches(user_text, self._re_praise):
            candidates.extend((
                EmotionCandidate("HAPPY", "用户夸赞", 26, 1.3),
                EmotionCandidate("SHY", "被夸到", 18, 1.0),
            ))
        if self._matches(text, self._re_surprise):
            candidates.append(EmotionCandidate("SURPRISED", "惊讶语义", 45, 1.4))
        if self._matches(text, self._re_anger):
            candidates.append(EmotionCandidate("ANGRY", "生气/护短语义", 38, 1.2))
        if self._matches(text, self._re_food):
            candidates.append(EmotionCandidate("EATING", "食物或热量话题", 35, 1.2))
        if self._matches(text, self._re_drink):
            candidates.append(EmotionCandidate("DRINKING", "喝水/补水话题", 38, 1.2))
        if self._matches(text, self._re_celebrate):
            candidates.extend((
                EmotionCandidate("CELEBRATE", "完成或好消息", 46, 1.4),
                EmotionCandidate("HAPPY", "好消息也会开心", 22, 1.1),
            ))
        if self._matches(text, self._re_work):
            candidates.append(EmotionCandidate("WORKING", "任务/代码语义", 22, 1.2))
        if self._matches(text, self._re_reading):
            candidates.append(EmotionCandidate("READING", "学习/资料语义", 24, 1.2))
        if self._matches(text, self._re_cool):
            candidates.append(EmotionCandidate("COOL", "装酷语义", 32, 1.1))
        if self.is_late_night(now):
            candidates.extend((
                EmotionCandidate("SLEEPY", "深夜更容易困", 18, 1.4),
                EmotionCandidate("GOOD_NIGHT", "深夜更容易晚安", 14, 1.2),
            ))

        normalized_model = self.normalize_emotion(model_emotion)
        if normalized_model in SEMANTIC_EMOTIONS and normalized_model != "NEUTRAL":
            candidates.append(
                EmotionCandidate(normalized_model, "模型给出的情绪建议", 8, 0.9)
            )
        return candidates

    def choose(
        self,
        candidates: Iterable[EmotionCandidate],
        now: datetime | None = None,
    ) -> EmotionResolution | None:
        """Select among available candidates by priority and weighted random."""
        now = now or datetime.now()
        ranked: list[tuple[EmotionCandidate, int, float]] = []
        for candidate in candidates:
            emotion = self.normalize_emotion(candidate.emotion)
            if emotion not in SEMANTIC_EMOTIONS:
                continue
            if not self.is_available(emotion, now):
                continue
            rank = EMOTION_RANKS[emotion]
            priority = rank.priority + candidate.priority_bonus
            weight = max(0.01, rank.weight * candidate.weight_multiplier)
            ranked.append((
                EmotionCandidate(
                    emotion,
                    candidate.reason,
                    candidate.priority_bonus,
                    candidate.weight_multiplier,
                ),
                priority,
                weight,
            ))

        if not ranked:
            return None

        top_priority = max(priority for _, priority, _ in ranked)
        pool = [
            (candidate, weight)
            for candidate, priority, weight in ranked
            if priority >= top_priority - 10
        ]
        total_weight = sum(weight for _, weight in pool)
        pick = self.rng.uniform(0, total_weight)
        upto = 0.0
        chosen = pool[-1][0]
        for candidate, weight in pool:
            upto += weight
            if pick <= upto:
                chosen = candidate
                break
        return self.mark(chosen.emotion, now, chosen.reason)

    def mark(
        self,
        emotion: str,
        now: datetime | None = None,
        reason: str = "",
    ) -> EmotionResolution:
        """Record a valid resolved expression, with an invalid-label fallback."""
        now = now or datetime.now()
        normalized = self.normalize_emotion(emotion)
        if normalized not in VALID_EMOTIONS:
            normalized = "NEUTRAL"
        self.last_emotion_at[normalized] = now
        return EmotionResolution(normalized, reason)

    def is_available(self, emotion: str, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        normalized = self.normalize_emotion(emotion)
        if normalized not in SEMANTIC_EMOTIONS:
            return False
        if normalized == "NEUTRAL":
            return True
        last_time = self.last_emotion_at.get(normalized)
        if last_time is None:
            return True
        cooldown_ms = EMOTION_RANKS[normalized].cooldown_ms
        return (now - last_time).total_seconds() * 1000 >= cooldown_ms

    @staticmethod
    def _compile(patterns: Iterable[str] | str) -> re.Pattern[str]:
        if isinstance(patterns, str):
            patterns = (patterns,)
        return re.compile("|".join(patterns))

    @staticmethod
    def _matches(text: str, pattern: re.Pattern[str]) -> bool:
        return pattern.search(text) is not None
