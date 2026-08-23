"""ASR -> brain -> TTS orchestration shared by HTTP and WebSocket APIs."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Awaitable, Callable

from audio.asr.base_asr import ASREngine
from audio.asr.whisper_asr import FasterWhisperEngine
from audio.tts.base_tts import TTSEngine
from audio.tts.edge_tts_engine import EDGE_VOICES, EdgeTTSEngine
from audio.tts.voxcpm_engine import FallbackTTSEngine, VoxCPMTTSEngine
from core.config import PetConfig
from core.client import StreamCancelled
from core.pet_brain import PetBrain
from systems.character_manager import DEFAULT_CHARACTER

from .audio import normalize_audio_payload, normalize_tts_wav

logger = logging.getLogger(__name__)

EventSink = Callable[[dict[str, object]], Awaitable[None]]


@dataclass(frozen=True)
class PipelineResult:
    session_id: str
    device_id: str
    character_id: str
    transcript: str
    emotion: str
    reply: str
    audio: bytes
    audio_format: str


class PipelineCancelled(RuntimeError):
    """Raised when a host request is cancelled before completion."""


class HostPipeline:
    """Single-flight host pipeline.

    ``PetBrain`` owns mutable conversation and long-term memory, so requests
    are serialized. ASR and TTS are moved off the asyncio event loop because
    both existing engines expose synchronous APIs.
    """

    def __init__(
        self,
        brain: PetBrain,
        asr_engine: ASREngine,
        tts_engine: TTSEngine,
        *,
        character_id: str = DEFAULT_CHARACTER,
    ) -> None:
        self.brain = brain
        self.asr_engine = asr_engine
        self.tts_engine = tts_engine
        self.character_id = character_id
        self._lock = asyncio.Lock()
        self._active_cancellations: dict[str, threading.Event] = {}
        self._cancellation_lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        config: PetConfig | None = None,
        *,
        character_id: str = DEFAULT_CHARACTER,
        asr_model: str = "small",
        asr_device: str = "cpu",
        asr_compute_type: str = "int8",
    ) -> "HostPipeline":
        config = config or PetConfig()
        brain = PetBrain(config, character=character_id)
        profile = brain.character_manager.get_profile(character_id)
        tts_config = profile.get("tts", {})
        edge_voice_key = tts_config.get(
            "fallback_voice", tts_config.get("voice", "yunxi")
        )
        edge_voice = EDGE_VOICES.get(edge_voice_key, edge_voice_key)
        edge_engine = EdgeTTSEngine(voice=edge_voice)

        if tts_config.get("engine") == "voxcpm":
            primary = VoxCPMTTSEngine(
                api_url=tts_config.get("api_url", "http://127.0.0.1:9890"),
                inference_steps=int(tts_config.get("inference_steps", 4)),
                timeout=float(tts_config.get("timeout_seconds", 120)),
                voice_id=str(tts_config.get("voice", "laoshaozi-personal")),
            )
            tts_engine: TTSEngine = FallbackTTSEngine(primary, edge_engine)
        else:
            tts_engine = edge_engine

        asr_engine = FasterWhisperEngine(
            model_size=asr_model,
            device=asr_device,
            compute_type=asr_compute_type,
        )
        return cls(
            brain,
            asr_engine,
            tts_engine,
            character_id=character_id,
        )

    @property
    def asr_name(self) -> str:
        return self.asr_engine.engine_name

    @property
    def tts_name(self) -> str:
        return self.tts_engine.engine_name

    def register_session(self, session_id: str) -> threading.Event:
        event = threading.Event()
        with self._cancellation_lock:
            self._active_cancellations[session_id] = event
        return event

    def cancel_session(self, session_id: str) -> bool:
        with self._cancellation_lock:
            event = self._active_cancellations.get(session_id)
        if event is None:
            return False
        event.set()
        return True

    def _unregister_session(self, session_id: str) -> None:
        with self._cancellation_lock:
            self._active_cancellations.pop(session_id, None)

    @staticmethod
    def _check_cancel(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise PipelineCancelled("session cancelled")

    async def process_audio(
        self,
        payload: bytes,
        *,
        session_id: str,
        device_id: str,
        content_type: str = "",
        sample_rate: int = 16_000,
        channels: int = 1,
        bits_per_sample: int = 16,
        emit: EventSink | None = None,
    ) -> PipelineResult:
        cancel_event = self.register_session(session_id)
        wav_path: str | None = None

        async def event(event_type: str, **data: object) -> None:
            if emit is not None:
                await emit({"type": event_type, "session_id": session_id, **data})

        try:
            wav_bytes = normalize_audio_payload(
                payload,
                content_type=content_type,
                sample_rate=sample_rate,
                channels=channels,
                bits_per_sample=bits_per_sample,
            )
            async with self._lock:
                self._check_cancel(cancel_event)
                await event("state", state="THINKING")

                with tempfile.NamedTemporaryFile(
                    mode="wb", suffix=".wav", delete=False
                ) as wav_file:
                    wav_file.write(wav_bytes)
                    wav_path = wav_file.name

                transcript = await asyncio.to_thread(
                    self.asr_engine.transcribe, wav_path
                )
                self._check_cancel(cancel_event)
                transcript = transcript.strip()
                await event("transcript", text=transcript)
                if not transcript:
                    raise ValueError("没有听清语音")

                try:
                    reply = await asyncio.to_thread(
                        self.brain.process_stream,
                        transcript,
                        None,
                        None,
                        cancel_event,
                    )
                except StreamCancelled as exc:
                    raise PipelineCancelled("session cancelled") from exc
                self._check_cancel(cancel_event)
                await event("emotion", emotion=reply.emotion)
                await event("reply", text=reply.content)

                audio = await asyncio.to_thread(
                    self.tts_engine.synthesize, reply.content
                )
                self._check_cancel(cancel_event)
                audio_format = self.tts_engine.audio_format
                audio, audio_format = normalize_tts_wav(audio, audio_format)
                await event(
                    "audio_ready",
                    format=audio_format,
                    bytes=len(audio),
                )
                await event("state", state="TALKING")
                return PipelineResult(
                    session_id=session_id,
                    device_id=device_id,
                    character_id=self.character_id,
                    transcript=transcript,
                    emotion=reply.emotion,
                    reply=reply.content,
                    audio=audio,
                    audio_format=audio_format,
                )
        finally:
            self._unregister_session(session_id)
            if wav_path:
                try:
                    os.unlink(wav_path)
                except OSError:
                    logger.warning("Unable to remove temporary WAV %s", wav_path)

    def close(self) -> None:
        self.brain.close(wait=False)
