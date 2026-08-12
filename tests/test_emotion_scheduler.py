from __future__ import annotations

import random
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from systems.emotion_scheduler import (  # noqa: E402
    EMOTION_PROFILES,
    PetEmotionScheduler,
)


class EmotionSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = PetEmotionScheduler(rng=random.Random(7))
        self.now = datetime(2026, 5, 11, 14, 0, 0)
        self.scheduler.start_idle_clock(self.now)

    def test_user_input_then_ai_start_uses_state_machine(self):
        listening = self.scheduler.on_user_input("我今天有点累", self.now)
        thinking = self.scheduler.on_ai_start(self.now + timedelta(milliseconds=600))

        self.assertEqual(listening.emotion, "LISTENING")
        self.assertEqual(thinking.emotion, "THINKING")
        self.assertEqual(self.scheduler.phase, "THINKING")

    def test_reply_without_trigger_returns_neutral(self):
        decision = self.scheduler.on_reply(
            "今天天气怎么样",
            "呆呆宝宝，今天多云。",
            "NEUTRAL",
            self.now + timedelta(seconds=2),
        )

        self.assertEqual(decision.emotion, "NEUTRAL")

    def test_tired_semantics_pick_tired_expression(self):
        decision = self.scheduler.on_reply(
            "我今天真的累趴了，压力好大",
            "抱抱你，先缓一口气。",
            "NEUTRAL",
            self.now + timedelta(seconds=2),
        )

        self.assertEqual(decision.emotion, "TIRED")
        self.assertGreater(EMOTION_PROFILES[decision.emotion].duration_ms, 0)

    def test_repeated_praise_triggers_heart_then_cooldown_avoids_repeat(self):
        for i, text in enumerate(("你真棒", "你太厉害了", "你真的好可爱")):
            self.scheduler.on_user_input(text, self.now + timedelta(seconds=i * 20))

        first = self.scheduler.on_reply(
            "你真的好可爱",
            "被你夸到啦。",
            "HAPPY",
            self.now + timedelta(seconds=62),
        )
        second = self.scheduler.on_reply(
            "继续夸夸你",
            "再夸我就要飘了。",
            "HAPPY",
            self.now + timedelta(seconds=63),
        )

        self.assertEqual(first.emotion, "HEART")
        self.assertNotEqual(second.emotion, "HEART")

    def test_idle_trigger_uses_ambient_emotion_after_threshold(self):
        decision = self.scheduler.maybe_idle(self.now + timedelta(seconds=200))

        self.assertIsNotNone(decision)
        # New idle pool includes SLEEPY, READING, GREETING, EATING, DRINKING, and rare COOL/HAPPY/SHY
        self.assertIn(decision.emotion, {"SLEEPY", "READING", "GREETING", "EATING", "DRINKING", "COOL", "HAPPY", "SHY"})

    def test_late_night_favors_sleepy_or_good_night(self):
        late = datetime(2026, 5, 11, 1, 30, 0)
        decision = self.scheduler.default_decision(late)

        self.assertIn(decision.emotion, {"SLEEPY", "GOOD_NIGHT", "NEUTRAL"})
        self.assertNotEqual(decision.emotion, "WORKING")


if __name__ == "__main__":
    unittest.main()
