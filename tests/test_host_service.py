from __future__ import annotations

import asyncio
import io
import sys
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_service.audio import (  # noqa: E402
    normalize_audio_payload,
    normalize_tts_wav,
    pcm_to_wav,
)
from host_service.pipeline import HostPipeline  # noqa: E402


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
            ["state", "transcript", "emotion", "reply", "audio_ready", "state"],
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


if __name__ == "__main__":
    unittest.main()
