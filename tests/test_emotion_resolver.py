from __future__ import annotations

import random
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_service.pipeline import HostPipeline  # noqa: E402
from systems.emotion_resolver import (  # noqa: E402
    STATE_CONTROLLED_EMOTIONS,
    EmotionResolver,
)
from systems.emotion_scheduler import PetEmotionScheduler  # noqa: E402


class _UnusedBrain:
    def close(self, wait=False):
        return None


class _UnusedASR:
    engine_name = "unused-asr"


class _UnusedTTS:
    engine_name = "unused-tts"


def _host_pipeline(character_id: str, seed: int = 7) -> HostPipeline:
    return HostPipeline(
        _UnusedBrain(),
        _UnusedASR(),
        _UnusedTTS(),
        character_id=character_id,
        emotion_resolver=EmotionResolver(random.Random(seed)),
    )


class EmotionResolverTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 11, 14, 0, 0)

    def test_desktop_and_hardware_make_identical_decisions(self):
        desktop = PetEmotionScheduler(
            resolver=EmotionResolver(random.Random(19))
        )
        hardware = _host_pipeline("personal", seed=19)
        turns = (
            ("代码终于成功完成了", "一切顺利。", "SAD"),
            ("普通话题", "收到。", "invalid-label"),
            ("你真棒", "谢谢。", "NEUTRAL"),
            ("你太厉害了", "被你夸到了。", "HAPPY"),
            ("你真的好可爱", "再夸就要飘了。", "HAPPY"),
        )

        for index, (user_text, assistant_text, model_emotion) in enumerate(turns):
            user_at = self.now + timedelta(seconds=index * 20)
            reply_at = user_at + timedelta(seconds=1)
            desktop.on_user_input(user_text, user_at)
            hardware.emotion_resolver.observe_user_input(user_text, user_at)

            desktop_decision = desktop.on_reply(
                user_text,
                assistant_text,
                model_emotion,
                reply_at,
            )
            hardware_decision = hardware.emotion_resolver.resolve(
                user_text,
                assistant_text,
                model_emotion,
                reply_at,
            )

            with self.subTest(index=index, user_text=user_text):
                self.assertEqual(
                    desktop_decision.emotion,
                    hardware_decision.emotion,
                )
                self.assertEqual(
                    desktop_decision.reason,
                    hardware_decision.reason,
                )

    def test_invalid_and_neutral_model_labels_fall_back_to_neutral(self):
        for model_emotion in (None, "", "NEUTRAL", "not-a-real-emotion"):
            with self.subTest(model_emotion=model_emotion):
                resolver = EmotionResolver(random.Random(7))
                decision = resolver.resolve(
                    "普通话题",
                    "收到。",
                    model_emotion,
                    self.now,
                )
                self.assertEqual(decision.emotion, "NEUTRAL")

    def test_alias_and_model_emotion_are_fused_with_semantic_candidates(self):
        resolver = EmotionResolver(random.Random(7))
        alias_decision = resolver.resolve(
            "普通话题",
            "收到。",
            "难过",
            self.now,
        )
        semantic_decision = EmotionResolver(random.Random(7)).resolve(
            "代码终于成功完成了",
            "一切顺利。",
            "SAD",
            self.now,
        )

        self.assertEqual(alias_decision.emotion, "SAD")
        self.assertEqual(semantic_decision.emotion, "CELEBRATE")

    def test_state_machine_emotions_are_never_semantic_results(self):
        self.assertEqual(
            STATE_CONTROLLED_EMOTIONS,
            {"LISTENING", "THINKING"},
        )
        for model_emotion in ("LISTENING", "THINKING", "听你说", "思考"):
            with self.subTest(model_emotion=model_emotion):
                decision = EmotionResolver(random.Random(7)).resolve(
                    "普通话题",
                    "收到。",
                    model_emotion,
                    self.now,
                )
                self.assertEqual(decision.emotion, "NEUTRAL")

    def test_consecutive_praise_and_window_expiry(self):
        resolver = EmotionResolver(random.Random(7))
        for index, user_text in enumerate(("你真棒", "你太厉害了", "你好可爱")):
            resolver.observe_user_input(
                user_text,
                self.now + timedelta(seconds=index * 20),
            )
        decision = resolver.resolve(
            "你好可爱",
            "被你夸到了。",
            "HAPPY",
            self.now + timedelta(seconds=61),
        )

        expired = EmotionResolver(random.Random(7))
        expired.observe_user_input("你真棒", self.now)
        expired.observe_user_input(
            "你太厉害了",
            self.now + timedelta(seconds=181),
        )

        self.assertEqual(resolver.praise_streak, 3)
        self.assertEqual(decision.emotion, "HEART")
        self.assertEqual(expired.praise_streak, 1)

    def test_late_night_rule_and_explicit_skip(self):
        late = datetime(2026, 5, 11, 1, 30, 0)
        daytime = EmotionResolver(random.Random(7)).resolve_default(self.now)
        night = EmotionResolver(random.Random(7)).resolve_default(late)
        skipped = EmotionResolver(random.Random(7)).resolve_default(
            late,
            skip_night=True,
        )

        self.assertEqual(daytime.emotion, "NEUTRAL")
        self.assertIn(night.emotion, {"SLEEPY", "GOOD_NIGHT"})
        self.assertEqual(skipped.emotion, "NEUTRAL")

    def test_character_pipelines_do_not_share_resolver_state(self):
        personal = _host_pipeline("personal")
        shinchan = _host_pipeline("shinchan")
        for index, praise in enumerate(("你真棒", "你太厉害了", "你好可爱")):
            personal.emotion_resolver.observe_user_input(
                praise,
                self.now + timedelta(seconds=index * 20),
            )

        personal_decision = personal.emotion_resolver.resolve(
            "普通话题",
            "收到。",
            "NEUTRAL",
            self.now + timedelta(seconds=61),
        )
        shinchan_decision = shinchan.emotion_resolver.resolve(
            "普通话题",
            "收到。",
            "NEUTRAL",
            self.now + timedelta(seconds=61),
        )

        self.assertIsNot(
            personal.emotion_resolver,
            shinchan.emotion_resolver,
        )
        self.assertEqual(personal.emotion_resolver.praise_streak, 3)
        self.assertEqual(shinchan.emotion_resolver.praise_streak, 0)
        self.assertEqual(personal_decision.emotion, "HEART")
        self.assertEqual(shinchan_decision.emotion, "NEUTRAL")


if __name__ == "__main__":
    unittest.main()
