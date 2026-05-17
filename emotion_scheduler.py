# -*- coding: utf-8 -*-
"""
Pet Emotion Scheduler - Refactored to use the new state machine system.

This module retains the same public API (PetEmotionScheduler class) for
backward compatibility, but internally delegates state management to the
systems.state_machine.StateMachine and idle scheduling to
systems.idle_scheduler.IdleScheduler via the ExpressionManager.

For new code, prefer using systems.expression_manager.ExpressionManager directly.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from systems.expression_manager import ExpressionManager
from systems.idle_scheduler import IdleAction
from systems.state_machine import PetState


# =============================================================================
# Emotion Definitions (unchanged public API)
# =============================================================================

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

VALID_EMOTIONS = set(EMOTION_ASSETS)

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


# =============================================================================
# Data Classes (unchanged public API)
# =============================================================================

@dataclass(frozen=True)
class EmotionProfile:
    key: str
    category: str
    priority: int
    duration_ms: int
    cooldown_ms: int
    weight: float = 1.0


EMOTION_PROFILES = {
    "NEUTRAL": EmotionProfile("NEUTRAL", "HIGH", 10, 0, 0, 8.0),
    "LISTENING": EmotionProfile("LISTENING", "HIGH", 95, 900, 1500, 6.0),
    "THINKING": EmotionProfile("THINKING", "HIGH", 100, 0, 1200, 6.0),
    "WORKING": EmotionProfile("WORKING", "HIGH", 45, 5200, 45000, 3.0),
    "GOOD_NIGHT": EmotionProfile("GOOD_NIGHT", "HIGH", 80, 6500, 240000, 2.0),
    "TIRED": EmotionProfile("TIRED", "HIGH", 78, 5200, 90000, 2.2),
    "HAPPY": EmotionProfile("HAPPY", "MEDIUM", 58, 3600, 8000, 3.2),
    "SHY": EmotionProfile("SHY", "MEDIUM", 68, 4300, 12000, 2.2),
    "SURPRISED": EmotionProfile("SURPRISED", "MEDIUM", 70, 3300, 12000, 2.0),
    "SAD": EmotionProfile("SAD", "MEDIUM", 74, 5200, 15000, 2.0),
    "LOVE": EmotionProfile("LOVE", "MEDIUM", 72, 4300, 15000, 1.8),
    "HEART": EmotionProfile("HEART", "MEDIUM", 82, 4800, 15000, 1.7),
    "ANGRY": EmotionProfile("ANGRY", "LOW", 55, 2800, 180000, 1.0),
    "COOL": EmotionProfile("COOL", "LOW", 48, 3800, 180000, 1.0),
    "EATING": EmotionProfile("EATING", "LOW", 42, 4500, 240000, 1.1),
    "DRINKING": EmotionProfile("DRINKING", "LOW", 42, 3800, 180000, 1.2),
    "CELEBRATE": EmotionProfile("CELEBRATE", "LOW", 70, 4600, 120000, 1.5),
    "SLEEPY": EmotionProfile("SLEEPY", "NIGHT", 76, 5600, 140000, 2.0),
    "READING": EmotionProfile("READING", "AMBIENT", 38, 6200, 180000, 2.4),
    "GREETING": EmotionProfile("GREETING", "AMBIENT", 50, 3600, 60000, 1.4),
}


@dataclass(frozen=True)
class EmotionCandidate:
    emotion: str
    reason: str
    priority_bonus: int = 0
    weight_multiplier: float = 1.0


@dataclass(frozen=True)
class EmotionDecision:
    emotion: str
    duration_ms: int
    reason: str
    category: str


# =============================================================================
# PetEmotionScheduler (refactored, backward-compatible API)
# =============================================================================

class PetEmotionScheduler:
    """State-machine based emotion scheduler for the desktop pet.

    Internally uses systems.state_machine.StateMachine for state management
    and systems.idle_scheduler.IdleScheduler for idle behavior.

    Public API is unchanged for backward compatibility.
    """

    idle_after_seconds = 180
    praise_window_seconds = 180

    # ── Keyword patterns ──────────────────────────────────────────────

    praise_patterns = (
        r"可爱|好可爱|萌|乖|喜欢|爱你|亲亲|抱抱|贴贴",
        r"厉害|太强|真棒|好棒|牛|聪明|靠谱|优秀|帮|帅气",
        r"谢谢|感谢|辛苦|你真好|夸赞|表扬",
    )
    tired_patterns = (
        r"好累|累死|累了|困|困死|熬夜|睡不着",
        r"崩溃|压力|撑不住|烦死|emo",
    )
    sad_patterns = (r"难过|委屈|伤心|想哭|不开心|低落|焦虑|害怕")
    love_patterns = (r"喜欢|爱你|想你|亲亲|抱抱|贴贴|心动")
    surprise_patterns = (r"惊讶|震惊|吓我|没想到|真的假的|不会吧|啊？|\?|wow|哇")
    anger_patterns = (r"生气|气死|火大|烦死|讨厌|不爽|离谱|过分")
    food_patterns = (r"吃|饭|饿|零食|奶茶|咖啡|热量|卡路里|外卖|宵夜")
    drink_patterns = (r"喝水|口渴|补水|水杯|饮料")
    celebrate_patterns = (r"完成|成功|通过|搞定|赢了|太好|庆祝|好消息|顺利")
    work_patterns = (r"代码|项目|修|改|写|实现|测试|bug|报错|工作|任务|帮我")
    reading_patterns = (r"学习|看书|资料|论文|复习|知识点|查一下")
    bedtime_patterns = (r"晚安|睡觉|睡了|梦里见|该睡|休息")
    cool_patterns = (r"酷|帅|装一下|墨镜|帅气")

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

        # Core systems
        self._expr_mgr = ExpressionManager()

        # Backward-compatible attributes
        self.phase = "IDLE"
        self.current_emotion = "NEUTRAL"
        self.last_interaction_at: datetime | None = None
        self.last_emotion_at: dict[str, datetime] = {}
        self.praise_streak = 0

        # Pre-compiled regex patterns (compiled once at init, reused on every match)
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

        self.last_praise_at: datetime | None = None

    # ── Public API (unchanged signatures) ────────────────────────────────

    def start_idle_clock(self, now: datetime | None = None) -> None:
        """Start idle timing after the pet has been shown."""
        now = now or datetime.now()
        self.last_interaction_at = now
        self._expr_mgr.last_interaction_at = now
        self._expr_mgr.idle_scheduler.start(now)

    @staticmethod
    def normalize_emotion(raw_emotion: str | None) -> str:
        raw = str(raw_emotion or "NEUTRAL").strip()
        return EMOTION_ALIASES.get(raw, re.sub(r"[\s-]+", "_", raw.upper()))

    @staticmethod
    def is_late_night(now: datetime) -> bool:
        return now.hour >= 23 or now.hour < 6

    def on_user_input(self, user_text: str, now: datetime | None = None) -> EmotionDecision:
        """Called when user types. Forces LISTENING."""
        now = now or datetime.now()
        self.last_interaction_at = now
        self._update_praise_streak(user_text, now)
        self.phase = "LISTENING"

        # Use expression manager
        self._expr_mgr.on_user_input()
        return self._mark("LISTENING", now, "用户输入，先认真听")

    def on_ai_start(self, now: datetime | None = None) -> EmotionDecision:
        """Called when AI begins generating. Forces THINKING."""
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
        """Called when AI response is complete."""
        now = now or datetime.now()
        self.last_interaction_at = now
        self.phase = "RESPONDING"

        candidates = self._semantic_candidates(user_text, assistant_text, model_emotion, now)
        decision = self._choose(candidates, now)

        if decision and decision.emotion != "NEUTRAL":
            self._expr_mgr.on_ai_response(user_text, assistant_text, model_emotion or "NEUTRAL")
        else:
            self._expr_mgr.on_ai_response(user_text, assistant_text, "NEUTRAL")

        if decision:
            return decision
        return self.default_decision(now, "回复完成，无强触发表情")

    def on_error(self, now: datetime | None = None) -> EmotionDecision:
        """Called when AI processing fails."""
        now = now or datetime.now()
        self.phase = "RESPONDING"
        self._expr_mgr.on_error()

        return self._choose(
            [
                EmotionCandidate("ANGRY", "处理失败，有点不爽", 45),
                EmotionCandidate("TIRED", "处理失败，累趴一下", 20),
            ],
            now,
        ) or self.default_decision(now, "处理失败但表情在冷却")

    def maybe_idle(self, now: datetime | None = None) -> EmotionDecision | None:
        """Called periodically. Uses new IdleScheduler for natural idle behavior.

        The idle scheduler runs with these parameters:
        - Idle tick interval: 15-40 seconds
        - Action duration: 2-6 seconds
        - Inactivity progression: 60s→READING, 180s→SLEEPY, 600s→TIRED
        """
        now = now or datetime.now()

        if self.phase in {"LISTENING", "THINKING"}:
            return None

        if not self._expr_mgr.is_idle():
            return None

        # Delegate to the new IdleScheduler via ExpressionManager
        result = self._expr_mgr.on_idle_tick(now)
        if result is None:
            return None

        # Convert to EmotionDecision for backward compatibility
        if result == "NEUTRAL":
            self.last_interaction_at = now
            return self.default_decision(now, "idle动作结束，回到待机")

        self.last_interaction_at = now
        return self._mark(result, now, f"idle调度器触发: {result}")

    def default_decision(
        self, now: datetime | None = None, reason: str = "????",
        skip_night: bool = False,
    ) -> EmotionDecision:
        """Return to NEUTRAL (or night variant).

        Args:
            skip_night: True ??????? SLEEPY/GOOD_NIGHT??????????
        """
        now = now or datetime.now()
        self.phase = "IDLE"

        if self.is_late_night(now) and not skip_night:
            decision = self._choose(
                [
                    EmotionCandidate("SLEEPY", "???????", 8, 1.6),
                    EmotionCandidate("GOOD_NIGHT", "???????", 8, 1.2),
                    EmotionCandidate("NEUTRAL", reason, 0, 3.0),
                ],
                now,
            )
            if decision:
                return decision

        return self._mark("NEUTRAL", now, reason)

    # ── Internal: emotion matching ──────────────────────────────────────

    def _semantic_candidates(
        self,
        user_text: str,
        assistant_text: str,
        model_emotion: str | None,
        now: datetime,
    ) -> list[EmotionCandidate]:
        """Build emotion candidates from keyword matching."""
        text = f"{user_text}\n{assistant_text}"
        candidates: list[EmotionCandidate] = []

        if self._matches(user_text, self._re_bedtime):
            candidates.extend([
                EmotionCandidate("GOOD_NIGHT", "用户提到晚安/睡觉", 58, 1.6),
                EmotionCandidate("SLEEPY", "用户提到困意", 45, 1.4),
            ])
        if self._matches(text, self._re_tired):
            candidates.extend([
                EmotionCandidate("TIRED", "用户显得很累", 48, 1.5),
                EmotionCandidate("SAD", "用户需要安慰", 28, 1.2),
                EmotionCandidate("LISTENING", "先认真听用户说完", 18, 1.0),
            ])
        if self._matches(text, self._re_sad):
            candidates.extend([
                EmotionCandidate("SAD", "用户情绪低落", 45, 1.4),
                EmotionCandidate("HEART", "给用户一点鼓励", 28, 1.0),
                EmotionCandidate("LISTENING", "陪用户说说", 20, 1.0),
            ])
        if self._matches(text, self._re_love):
            candidates.extend([
                EmotionCandidate("LOVE", "亲密/喜欢语义", 46, 1.4),
                EmotionCandidate("HEART", "亲密时比心", 35, 1.0),
                EmotionCandidate("SHY", "被喜欢时害羞", 24, 1.0),
            ])
        if self.praise_streak >= 3:
            candidates.extend([
                EmotionCandidate("HEART", "连续夸赞触发比心", 70, 1.8),
                EmotionCandidate("SHY", "连续夸赞有点害羞", 55, 1.5),
                EmotionCandidate("HAPPY", "连续夸赞超开心", 42, 1.4),
            ])
        elif self.praise_streak >= 2:
            candidates.extend([
                EmotionCandidate("SHY", "连续夸赞，害羞一下", 42, 1.4),
                EmotionCandidate("HAPPY", "连续夸赞，开心一下", 28, 1.2),
            ])
        elif self._matches(user_text, self._re_praise):
            candidates.extend([
                EmotionCandidate("HAPPY", "用户夸赞", 26, 1.3),
                EmotionCandidate("SHY", "被夸到", 18, 1.0),
            ])
        if self._matches(text, self._re_surprise):
            candidates.append(EmotionCandidate("SURPRISED", "惊讶语义", 45, 1.4))
        if self._matches(text, self._re_anger):
            candidates.extend([
                EmotionCandidate("ANGRY", "生气/护短语义", 38, 1.2),
                EmotionCandidate("LISTENING", "先听用户吐槽", 16, 1.0),
            ])
        if self._matches(text, self._re_food):
            candidates.append(EmotionCandidate("EATING", "食物或热量话题", 35, 1.2))
        if self._matches(text, self._re_drink):
            candidates.append(EmotionCandidate("DRINKING", "喝水/补水话题", 38, 1.2))
        if self._matches(text, self._re_celebrate):
            candidates.extend([
                EmotionCandidate("CELEBRATE", "完成或好消息", 46, 1.4),
                EmotionCandidate("HAPPY", "好消息也会开心", 22, 1.1),
            ])
        if self._matches(text, self._re_work):
            candidates.append(EmotionCandidate("WORKING", "任务/代码语义", 22, 1.2))
        if self._matches(text, self._re_reading):
            candidates.append(EmotionCandidate("READING", "学习/资料语义", 24, 1.2))
        if self._matches(text, self._re_cool):
            candidates.append(EmotionCandidate("COOL", "装酷语义", 32, 1.1))
        if self.is_late_night(now):
            candidates.extend([
                EmotionCandidate("SLEEPY", "深夜更容易困", 18, 1.4),
                EmotionCandidate("GOOD_NIGHT", "深夜更容易晚安", 14, 1.2),
            ])

        normalized_model = self.normalize_emotion(model_emotion)
        if normalized_model in VALID_EMOTIONS and normalized_model != "NEUTRAL":
            candidates.append(
                EmotionCandidate(normalized_model, "模型给出的情绪建议", 8, 0.9)
            )

        return candidates

    def _choose(
        self, candidates: Iterable[EmotionCandidate], now: datetime
    ) -> EmotionDecision | None:
        """Priority + weighted random selection from candidates."""
        ranked: list[tuple[EmotionCandidate, int, float]] = []
        for candidate in candidates:
            emotion = self.normalize_emotion(candidate.emotion)
            if emotion not in EMOTION_PROFILES:
                continue
            if not self._is_available(emotion, now):
                continue
            profile = EMOTION_PROFILES[emotion]
            priority = profile.priority + candidate.priority_bonus
            weight = max(0.01, profile.weight * candidate.weight_multiplier)
            ranked.append((
                EmotionCandidate(emotion, candidate.reason, candidate.priority_bonus, candidate.weight_multiplier),
                priority,
                weight,
            ))

        if not ranked:
            return None

        top_priority = max(priority for _, priority, _ in ranked)
        pool = [
            (candidate, priority, weight)
            for candidate, priority, weight in ranked
            if priority >= top_priority - 10
        ]
        total_weight = sum(weight for _, _, weight in pool)
        pick = self.rng.uniform(0, total_weight)
        upto = 0.0
        chosen = pool[-1][0]
        for candidate, _, weight in pool:
            upto += weight
            if pick <= upto:
                chosen = candidate
                break
        return self._mark(chosen.emotion, now, chosen.reason)

    def _mark(self, emotion: str, now: datetime, reason: str) -> EmotionDecision:
        """Record and return an emotion decision."""
        emotion = self.normalize_emotion(emotion)
        profile = EMOTION_PROFILES.get(emotion, EMOTION_PROFILES["NEUTRAL"])
        self.current_emotion = profile.key
        self.last_emotion_at[profile.key] = now
        return EmotionDecision(
            emotion=profile.key,
            duration_ms=profile.duration_ms,
            reason=reason,
            category=profile.category,
        )

    def _is_available(self, emotion: str, now: datetime) -> bool:
        """Check if an emotion's cooldown has expired."""
        if emotion == "NEUTRAL":
            return True
        last_time = self.last_emotion_at.get(emotion)
        if last_time is None:
            return True
        profile = EMOTION_PROFILES[emotion]
        return (now - last_time).total_seconds() * 1000 >= profile.cooldown_ms

    def _update_praise_streak(self, user_text: str, now: datetime) -> None:
        """Track consecutive praise for streak bonuses."""
        if not self._matches(user_text, self._re_praise):
            return
        if self.last_praise_at is None:
            self.praise_streak = 1
        elif (now - self.last_praise_at).total_seconds() <= self.praise_window_seconds:
            self.praise_streak += 1
        else:
            self.praise_streak = 1
        self.last_praise_at = now

    @staticmethod
    def _compile(patterns: Iterable[str] | str) -> re.Pattern:
        """Compile one or more regex patterns into a single compiled pattern."""
        if isinstance(patterns, str):
            patterns = (patterns,)
        return re.compile("|".join(patterns))


    @staticmethod
    @staticmethod
    def _matches(text: str, pattern: re.Pattern) -> bool:
        """Check if text matches the compiled regex pattern."""
        return pattern.search(text) is not None

