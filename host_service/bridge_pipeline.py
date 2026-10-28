"""Microphone -> Whisper -> qq-bridge. No PetBrain/client/history is created."""
from __future__ import annotations

import asyncio
import json
import os
import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx
import numpy as np

from audio.asr.whisper_asr import FasterWhisperEngine
from audio.tts.qwen_engine import QwenTTSEngine, resolve_tts_config
from systems.character_manager import CharacterManager
from .audio import validate_audio_parameters
from .pipeline import HostPipeline, PipelineCancelled, PipelineResult


class BridgePipeline(HostPipeline):
    chat_backend = "qq-bridge"

    def __init__(self, asr_engine, tts_engine, channel_config, *, character_id="personal", character_manager=None,
                 client_factory=None):
        super().__init__(None, asr_engine, tts_engine, character_id=character_id)
        self.character_manager = character_manager or CharacterManager()
        self.channel_config = channel_config
        self._ready_inputs = set()
        self.client_factory = client_factory

    @classmethod
    def from_bridge_config(cls, config_path=None, *, character_id="personal", asr_model="base",
                           asr_device="cpu", asr_compute_type="int8"):
        path = Path(config_path or os.getenv("TOPPET_BRIDGE_CONFIG", r"D:\dafeiyu\qq-bridge\hardware.channel.local.json"))
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not config.get("enabled") or not str(config.get("userQQ", "")).isdigit():
            raise ValueError("qq-bridge hardware channel is not enabled or bound")
        manager = CharacterManager()
        voice = resolve_tts_config(manager.get_profile(character_id))
        if voice.get("engine") != "qwen":
            raise ValueError("hardware channel requires the configured Qwen voice")
        tts = QwenTTSEngine(api_url=voice.get("api_url", "http://127.0.0.1:9891"),
                            voice_id=voice.get("voice", "qwen-user-20261028"),
                            runtime=voice.get("qwen_runtime", "fast"),
                            timeout=float(voice.get("timeout_seconds", 180)))
        asr = FasterWhisperEngine(model_size=asr_model, device=asr_device, compute_type=asr_compute_type)
        return cls(asr, tts, config, character_id=character_id, character_manager=manager)

    def input_ready(self, session_id):
        return session_id in self._ready_inputs and not self.session_cancel_event(session_id).is_set()

    def _client(self):
        port = int(self.channel_config["port"])
        if not 1 <= port <= 65535:
            raise ValueError("invalid local qq-bridge port")
        token = Path(self.channel_config["gatewayTokenFile"]).read_text(encoding="utf-8").strip()
        options = {"base_url": f"http://127.0.0.1:{port}",
                   "headers": {"authorization": "Bearer " + token}, "timeout": 6.0, "trust_env": False}
        return self.client_factory(**options) if self.client_factory else httpx.AsyncClient(**options)

    async def process_audio(self, *args, **kwargs):
        raise ValueError("qq-bridge hardware chat requires the device WebSocket v2 channel")

    async def process_streamed_audio(self, pcm, *, session_id, device_id, sample_rate=16000,
                                     channels=1, bits_per_sample=16, emit=None, send_audio=None):
        validate_audio_parameters(sample_rate, channels, bits_per_sample)
        if device_id != self.channel_config["deviceId"]:
            raise ValueError("device is not bound to this qq-bridge user")
        if not pcm or len(pcm) % 2:
            raise ValueError("invalid or empty PCM")
        cancelled = self.session_cancel_event(session_id)
        submitted = False
        complete = False
        request = {"device_id": device_id, "session_id": session_id}
        try:
            async with self._lock:
                self._check_cancel(cancelled)
                samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
                logging.getLogger('toppet.bridge_input').info(
                    'input_pcm session=%s duration_ms=%d rms=%.1f peak=%.0f', session_id,
                    len(pcm) * 1000 // 32000, float(np.sqrt(np.mean(samples * samples))),
                    float(np.max(np.abs(samples))))
                self.latency.record("asr_started", session_id=session_id, device_id=device_id)
                transcript = (await asyncio.to_thread(self._transcribe_pcm, pcm, sample_rate)).strip()
                self._check_cancel(cancelled)
                if not transcript:
                    raise ValueError("没有听清语音")
                self.latency.record("asr_final", session_id=session_id, device_id=device_id)
                if emit:
                    await emit({"type": "stt_final", "session_id": session_id, "text": transcript})
                self._ready_inputs.add(session_id)
                async with self._client() as client:
                    submitted = True  # A timed-out POST may already have been accepted.
                    response = await client.post("/v1/hardware/turn", json={**request, "transcript": transcript})
                    response.raise_for_status()
                    turn = response.json()
                    deadline = asyncio.get_running_loop().time() + 115
                    while True:
                        self._check_cancel(cancelled)
                        if asyncio.get_running_loop().time() > deadline:
                            raise TimeoutError("qq-bridge hardware turn timed out")
                        response = await client.get("/v1/hardware/turn/" + turn["turn_id"])
                        response.raise_for_status()
                        result = response.json()
                        if result["state"] == "completed" and result.get("playback_confirmed") is True:
                            complete = True
                            return PipelineResult(session_id, device_id, self.character_id, transcript,
                                                  "NEUTRAL", result.get("reply") or "", b"", "pcm_s16le")
                        if result["state"] == "cancelled":
                            raise PipelineCancelled("hardware turn cancelled")
                        if result["state"] == "failed":
                            raise RuntimeError("qq-bridge hardware turn failed: " + str(result.get("error")))
                        await asyncio.sleep(.2)
        finally:
            self._ready_inputs.discard(session_id)
            if submitted and not complete:
                try:
                    async with self._client() as client:
                        await client.post("/v1/hardware/cancel", json=request)
                except (httpx.HTTPError, OSError):
                    pass
            self._unregister_session(session_id, cancelled)

    def close(self):
        for event in self._active_cancellations.values():
            event.set()
