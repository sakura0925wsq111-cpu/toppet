from __future__ import annotations

import asyncio
import io
import random
import sys
import unittest
import wave
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_service.audio import (  # noqa: E402
    normalize_audio_payload,
    normalize_tts_wav,
    pcm_to_wav,
)
from host_service.pipeline import HostPipeline  # noqa: E402
from host_service.server import create_app  # noqa: E402
from host_service.subtitle import (  # noqa: E402
    MASK_BYTES,
    build_subtitle_event,
    encode_zero_literal_rle,
    render_subtitle_mask,
)
from host_service.voxcpm_service import VoxCPMServiceController  # noqa: E402
from systems.emotion_resolver import EmotionResolver  # noqa: E402


class _FakeASR:
    engine_name = "fake-asr"

    def transcribe(self, path: str) -> str:
        with wave.open(path, "rb") as wav_file:
            assert wav_file.getframerate() == 16_000
            assert wav_file.getnchannels() == 1
            assert wav_file.getsampwidth() == 2
        return "你好，老勺子"


class _FakeTTS:
    engine_name = "fake-tts"
    audio_format = "wav"

    def synthesize(self, text: str) -> bytes:
        assert text == "收到啦。"
        return pcm_to_wav(b"\0\0" * 160)


class _FakeBrain:
    def process_stream(self, text, on_emotion=None, on_text_delta=None, cancel_event=None):
        assert text == "你好，老勺子"
        return SimpleNamespace(emotion="HAPPY", content="收到啦。")

    def close(self, wait=False):
        return None


class _FakeVoiceService:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start_and_wait(self):
        self.started = True

    def stop(self):
        self.stopped = True


class HostServiceTests(unittest.TestCase):
    def test_raw_pcm_is_wrapped_as_16khz_mono_wav(self):
        wav_bytes = normalize_audio_payload(b"\0\0" * 160)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            self.assertEqual(wav_file.getframerate(), 16_000)
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getnframes(), 160)

    def test_pipeline_emits_protocol_events_and_returns_audio(self):
        pipeline = HostPipeline(
            _FakeBrain(),
            _FakeASR(),
            _FakeTTS(),
            character_id="personal",
        )
        events: list[dict[str, object]] = []

        async def emit(event):
            events.append(event)

        result = asyncio.run(
            pipeline.process_audio(
                pcm_to_wav(b"\0\0" * 160),
                session_id="session-1",
                device_id="device-1",
                content_type="audio/wav",
                emit=emit,
            )
        )

        self.assertEqual(result.transcript, "你好，老勺子")
        self.assertEqual(result.emotion, "HAPPY")
        self.assertTrue(result.audio.startswith(b"RIFF"))
        self.assertEqual(
            [event["type"] for event in events],
            [
                "state",
                "transcript",
                "emotion",
                "reply",
                "subtitle",
                "audio_ready",
                "state",
            ],
        )
        self.assertEqual(events[0]["state"], "THINKING")
        self.assertEqual(events[-1]["state"], "TALKING")

    def test_tts_wav_is_downmixed_and_resampled_for_esp32(self):
        source = io.BytesIO()
        with wave.open(source, "wb") as wav_file:
            wav_file.setnchannels(2)
            wav_file.setsampwidth(2)
            wav_file.setframerate(48_000)
            wav_file.writeframes(b"\0\0\0\0" * 480)
        normalized, fmt = normalize_tts_wav(source.getvalue(), "wav")
        self.assertEqual(fmt, "wav")
        with wave.open(io.BytesIO(normalized), "rb") as wav_file:
            self.assertEqual(wav_file.getframerate(), 16_000)
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)

    def test_app_lifespan_starts_and_stops_owned_voxcpm_service(self):
        pipeline = HostPipeline(
            _FakeBrain(),
            _FakeASR(),
            _FakeTTS(),
            character_id="personal",
        )
        voice_service = _FakeVoiceService()
        app = create_app(pipeline, voxcpm_service=voice_service)

        async def exercise_lifespan():
            async with app.router.lifespan_context(app):
                self.assertTrue(voice_service.started)
                self.assertFalse(voice_service.stopped)

        asyncio.run(exercise_lifespan())
        self.assertTrue(voice_service.stopped)

    def test_voxcpm_controller_reuses_ready_service_without_spawning(self):
        controller = VoxCPMServiceController()
        with (
            patch.object(controller, "is_ready", return_value=True),
            patch("host_service.voxcpm_service.subprocess.Popen") as popen,
        ):
            controller.start_and_wait()
            controller.stop()

        popen.assert_not_called()
        self.assertFalse(controller._owned_process)

    def test_neutral_model_emotion_gets_semantic_hardware_fallback(self):
        cases = (
            ("你好呀", "今天过得怎么样？", "NEUTRAL"),
            ("我今天好难过", "抱抱你，我会陪着你。", "HEART"),
            ("这个功能完成了", "太棒了！", "CELEBRATE"),
            ("请检查这段代码", "我来认真检查。", "WORKING"),
        )
        for user_text, assistant_text, expected in cases:
            with self.subTest(expected=expected):
                pipeline = HostPipeline(
                    _FakeBrain(),
                    _FakeASR(),
                    _FakeTTS(),
                    character_id="personal",
                    emotion_resolver=EmotionResolver(random.Random(7)),
                )
                pipeline.emotion_resolver.observe_user_input(
                    user_text,
                    datetime(2026, 5, 11, 14, 0, 0),
                )
                self.assertEqual(
                    pipeline.emotion_resolver.resolve(
                        user_text,
                        assistant_text,
                        "NEUTRAL",
                        datetime(2026, 5, 11, 14, 0, 1),
                    ).emotion,
                    expected,
                )

    def test_hardware_subtitle_mask_round_trips_through_rle(self):
        import base64

        mask = render_subtitle_mask("这是电脑端生成的真实回复字幕。")
        event = build_subtitle_event("这是电脑端生成的真实回复字幕。")
        encoded = base64.b64decode(event["data"])
        decoded = bytearray()
        offset = 0
        while offset < len(encoded):
            header = encoded[offset]
            offset += 1
            count = (header & 0x7F) + 1
            if header & 0x80:
                decoded.extend(b"\0" * count)
            else:
                decoded.extend(encoded[offset:offset + count])
                offset += count

        self.assertEqual(len(mask), MASK_BYTES)
        self.assertEqual(bytes(decoded), mask)
        self.assertLess(len(encode_zero_literal_rle(mask)), len(mask))


if __name__ == "__main__":
    unittest.main()
