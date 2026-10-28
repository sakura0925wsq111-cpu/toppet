from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.paths import CHARACTER_PROFILES_FILE  # noqa: E402
from audio.tts.base_tts import TTSEngine  # noqa: E402
from audio.tts.edge_tts_engine import EdgeTTSEngine  # noqa: E402
from audio.tts.voxcpm_engine import (  # noqa: E402
    FallbackTTSEngine,
    VoxCPMTTSEngine,
)
from systems.voxcpm_service_manager import VoxCPMServiceManager  # noqa: E402


class _Response:
    def __init__(self, status_code=200, content=b"RIFFtest", payload=None, text=""):
        self.status_code = status_code
        self.content = content
        self._payload = payload or {"status": "ready"}
        self.text = text

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response or _Response()
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class _Engine(TTSEngine):
    def __init__(self, result=b"ok", error=None, audio_format="wav"):
        self.result = result
        self.error = error
        self._format = audio_format
        self.calls = 0

    def synthesize(self, text: str) -> bytes:
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    @property
    def engine_name(self) -> str:
        return "fake"

    @property
    def audio_format(self) -> str:
        return self._format


class VoxCPMEngineTests(unittest.TestCase):
    def test_success_returns_wav_and_sends_configured_steps(self):
        session = _Session()
        engine = VoxCPMTTSEngine(
            "http://127.0.0.1:9890", inference_steps=6, session=session
        )
        self.assertEqual(engine.synthesize("你好"), b"RIFFtest")
        url, kwargs = session.calls[0]
        self.assertEqual(url, "http://127.0.0.1:9890/tts")
        self.assertEqual(kwargs["json"], {"text": "你好", "inference_steps": 6})

    def test_invalid_audio_is_rejected(self):
        engine = VoxCPMTTSEngine(session=_Session(_Response(content=b"not-wav")))
        with self.assertRaisesRegex(RuntimeError, "invalid WAV"):
            engine.synthesize("你好")

    def test_timeout_is_reported_clearly(self):
        engine = VoxCPMTTSEngine(
            timeout=0.1, session=_Session(error=requests.Timeout("slow"))
        )
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            engine.synthesize("你好")

    def test_primary_failure_uses_edge_compatible_fallback(self):
        primary = _Engine(error=RuntimeError("offline"))
        fallback = _Engine(result=b"mp3", audio_format="mp3")
        engine = FallbackTTSEngine(primary, fallback)
        self.assertEqual(engine.synthesize("继续说"), b"mp3")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(engine.audio_format, "mp3")

    def test_edge_fallback_retries_transient_no_audio_response(self):
        engine = EdgeTTSEngine(max_retries=2, retry_delay=0)
        synthesize = AsyncMock(
            side_effect=[RuntimeError("no audio"), RuntimeError("no audio"), b"mp3"]
        )
        with patch.object(engine, "_async_synthesize", synthesize):
            self.assertEqual(engine.synthesize("继续说"), b"mp3")
        self.assertEqual(synthesize.await_count, 3)


class VoxCPMRoleAndRuntimeTests(unittest.TestCase):
    def test_personal_role_selects_qwen_and_keeps_voxcpm_available(self):
        profiles = json.loads(CHARACTER_PROFILES_FILE.read_text(encoding="utf-8"))
        self.assertEqual(profiles["personal"]["tts"]["engine"], "qwen")
        self.assertTrue(profiles["personal"]["tts"]["optional_voxcpm"])
        self.assertFalse(profiles["personal"]["tts"]["allow_voice_fallback"])
        self.assertEqual(
            profiles["personal"]["tts"]["adaptive_inference_steps"], [3, 4]
        )
        self.assertEqual(profiles["shinchan"]["tts"]["engine"], "edge")

    def test_runtime_validation_rejects_missing_files(self):
        manager = VoxCPMServiceManager(
            runtime_python=r"Z:\missing\python.exe",
            server_script=r"Z:\missing\server.py",
            model_path=r"Z:\missing\model",
            reference_path=r"Z:\missing\reference.wav",
        )
        self.assertFalse(manager.runtime_available)


if __name__ == "__main__":
    unittest.main()
