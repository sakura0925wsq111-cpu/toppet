"""ASR -> brain -> TTS orchestration shared by HTTP and WebSocket APIs."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from typing import Awaitable, Callable

from audio.asr.base_asr import ASREngine
from audio.asr.whisper_asr import FasterWhisperEngine
from audio.tts.base_tts import TTSEngine
from audio.tts.edge_tts_engine import EDGE_VOICES, EdgeTTSEngine
from audio.tts.voxcpm_engine import AudioChunk, FallbackTTSEngine, VoxCPMTTSEngine
from core.config import PetConfig
from core.client import StreamCancelled
from core.pet_brain import PetBrain
from core.sentence_chunker import SentenceChunker
from systems.character_manager import DEFAULT_CHARACTER
from systems.emotion_resolver import EmotionResolver

from .audio import (
    normalize_audio_payload,
    normalize_tts_wav,
    validate_audio_parameters,
)
from .latency import LatencyTracker
from .subtitle import build_subtitle_event

logger = logging.getLogger(__name__)

EventSink = Callable[[dict[str, object]], Awaitable[None]]
AudioSink = Callable[[AudioChunk], Awaitable[None]]


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
        emotion_resolver: EmotionResolver | None = None,
        latency: LatencyTracker | None = None,
    ) -> None:
        self.brain = brain
        self.asr_engine = asr_engine
        self.tts_engine = tts_engine
        self.character_id = character_id
        self.emotion_resolver = emotion_resolver or EmotionResolver()
        self.latency = latency or LatencyTracker()
        self._lock = asyncio.Lock()
        self._active_cancellations: dict[str, threading.Event] = {}
        self._cancellation_lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        config: PetConfig | None = None,
        *,
        character_id: str = DEFAULT_CHARACTER,
        asr_model: str = "base",
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

    @property
    def requires_voxcpm(self) -> bool:
        return isinstance(self.tts_engine, FallbackTTSEngine) and isinstance(
            self.tts_engine.primary, VoxCPMTTSEngine
        )

    def register_session(self, session_id: str) -> threading.Event:
        event = threading.Event()
        with self._cancellation_lock:
            self._active_cancellations[session_id] = event
        return event

    def session_cancel_event(self, session_id: str) -> threading.Event:
        with self._cancellation_lock:
            event = self._active_cancellations.get(session_id)
            if event is None:
                event = threading.Event()
                self._active_cancellations[session_id] = event
        return event

    def cancel_session(self, session_id: str) -> bool:
        with self._cancellation_lock:
            event = self._active_cancellations.get(session_id)
        if event is None:
            return False
        event.set()
        return True

    def _unregister_session(
        self, session_id: str, cancel_event: threading.Event | None = None
    ) -> None:
        with self._cancellation_lock:
            current = self._active_cancellations.get(session_id)
            if cancel_event is None or current is cancel_event:
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
        self.latency.record(
            "session_created", session_id=session_id, device_id=device_id
        )

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
                asr_started = time.monotonic()
                self.latency.record(
                    "asr_started", session_id=session_id, device_id=device_id
                )
                transcript = await asyncio.to_thread(
                    self._transcribe_wav_in_memory, wav_bytes
                )
                self.latency.record(
                    "asr_final",
                    session_id=session_id,
                    device_id=device_id,
                    started_at=asr_started,
                )
                self._check_cancel(cancel_event)
                transcript = transcript.strip()
                await event("transcript", text=transcript)
                if not transcript:
                    raise ValueError("没有听清语音")
                self.emotion_resolver.observe_user_input(transcript)

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
                device_emotion = self.emotion_resolver.resolve(
                    transcript,
                    reply.content,
                    reply.emotion,
                ).emotion
                await event("emotion", emotion=device_emotion)
                await event("reply", text=reply.content)
                await event("subtitle", **build_subtitle_event(reply.content))

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
                    emotion=device_emotion,
                    reply=reply.content,
                    audio=audio,
                    audio_format=audio_format,
                )
        finally:
            self._unregister_session(session_id, cancel_event)

    async def process_streamed_audio(
        self,
        pcm: bytes,
        *,
        session_id: str,
        device_id: str,
        sample_rate: int = 16_000,
        channels: int = 1,
        bits_per_sample: int = 16,
        emit: EventSink | None = None,
        send_audio: AudioSink | None = None,
    ) -> PipelineResult:
        """Run ASR/LLM/TTS as a sentence and PCM streaming pipeline."""
        validate_audio_parameters(sample_rate, channels, bits_per_sample)
        if not pcm or len(pcm) % 2:
            raise ValueError("PCM payload is empty or ends on a partial sample")
        cancel_event = self.session_cancel_event(session_id)

        async def event(event_type: str, **data: object) -> None:
            if emit is not None:
                await emit({"type": event_type, "session_id": session_id, **data})

        try:
            async with self._lock:
                self._check_cancel(cancel_event)
                asr_started = time.monotonic()
                self.latency.record(
                    "asr_started", session_id=session_id, device_id=device_id
                )
                transcript = await asyncio.to_thread(
                    self._transcribe_pcm, pcm, sample_rate
                )
                transcript = transcript.strip()
                self.latency.record(
                    "asr_final",
                    session_id=session_id,
                    device_id=device_id,
                    started_at=asr_started,
                )
                self._check_cancel(cancel_event)
                await event("stt_final", text=transcript)
                if not transcript:
                    raise ValueError("没有听清语音")
                self.emotion_resolver.observe_user_input(transcript)

                loop = asyncio.get_running_loop()
                sentence_queue: asyncio.Queue[str | object] = asyncio.Queue()
                sentinel = object()
                chunker = SentenceChunker(min_chars=4, max_chars=18, comma_chars=10)
                callback_state: dict[str, object] = {
                    "first_token": False,
                    "first_sentence": False,
                    "emotion": None,
                }
                llm_started = time.monotonic()
                self.latency.record(
                    "llm_started", session_id=session_id, device_id=device_id
                )

                def enqueue_sentence(sentence: str) -> None:
                    sentence = sentence.strip()
                    if not sentence or cancel_event.is_set():
                        return
                    if not callback_state["first_sentence"]:
                        callback_state["first_sentence"] = True
                        self.latency.record(
                            "llm_first_sentence",
                            session_id=session_id,
                            device_id=device_id,
                            started_at=llm_started,
                        )
                    loop.call_soon_threadsafe(sentence_queue.put_nowait, sentence)

                def on_text_delta(delta: str) -> None:
                    if cancel_event.is_set() or not delta:
                        return
                    if not callback_state["first_token"] and delta.strip():
                        callback_state["first_token"] = True
                        self.latency.record(
                            "llm_first_token",
                            session_id=session_id,
                            device_id=device_id,
                            started_at=llm_started,
                        )
                    for sentence in chunker.feed(delta):
                        enqueue_sentence(sentence)

                def on_emotion(emotion: str) -> None:
                    if cancel_event.is_set():
                        return
                    callback_state["emotion"] = emotion
                    asyncio.run_coroutine_threadsafe(
                        event("emotion", emotion=emotion), loop
                    )

                def run_llm():
                    try:
                        return self.brain.process_stream(
                            transcript,
                            on_emotion,
                            on_text_delta,
                            cancel_event,
                        )
                    finally:
                        for sentence in chunker.flush():
                            enqueue_sentence(sentence)
                        loop.call_soon_threadsafe(sentence_queue.put_nowait, sentinel)

                llm_task = asyncio.create_task(asyncio.to_thread(run_llm))
                tts_started: float | None = None
                tts_sequence = [0]
                first_tts_chunk = [False]
                try:
                    while True:
                        sentence = await sentence_queue.get()
                        if sentence is sentinel:
                            break
                        self._check_cancel(cancel_event)
                        if tts_started is None:
                            tts_started = time.monotonic()
                            self.latency.record(
                                "tts_started",
                                session_id=session_id,
                                device_id=device_id,
                            )
                            await event(
                                "tts_start",
                                format="pcm_s16le",
                                sample_rate=16_000,
                                channels=1,
                                bits_per_sample=16,
                            )
                        sentence_text = str(sentence)
                        sentence_start_sequence = tts_sequence[0]
                        await event(
                            "tts_sentence_start",
                            text=sentence_text,
                            sequence=sentence_start_sequence,
                        )
                        await event(
                            "subtitle", **build_subtitle_event(sentence_text)
                        )
                        await asyncio.to_thread(
                            self._stream_sentence_to_sink,
                            sentence_text,
                            cancel_event,
                            loop,
                            send_audio,
                            session_id,
                            device_id,
                            tts_sequence,
                            first_tts_chunk,
                            tts_started,
                        )
                        # Preserve a natural punctuation pause while the
                        # single-GPU VoxCPM service starts the next sentence.
                        # These zero samples are real stream continuity, not a
                        # prompt or prerecorded speech, and prevent the device
                        # ring from hard-underrunning between model requests.
                        await asyncio.to_thread(
                            self._stream_silence_to_sink,
                            12,  # 12 x 20 ms = 240 ms
                            cancel_event,
                            loop,
                            send_audio,
                            tts_sequence,
                        )
                        await event(
                            "tts_sentence_end",
                            text=sentence_text,
                            start_sequence=sentence_start_sequence,
                            end_sequence=max(
                                sentence_start_sequence, tts_sequence[0] - 1
                            ),
                        )
                    reply = await llm_task
                except Exception:
                    cancel_event.set()
                    await asyncio.gather(llm_task, return_exceptions=True)
                    raise

                self._check_cancel(cancel_event)
                device_emotion = self.emotion_resolver.resolve(
                    transcript,
                    reply.content,
                    reply.emotion,
                ).emotion
                if callback_state["emotion"] != device_emotion:
                    await event("emotion", emotion=device_emotion)
                await event("reply", text=reply.content)
                await event("tts_stop", state="IDLE")
                if tts_started is not None:
                    self.latency.record(
                        "tts_finished",
                        session_id=session_id,
                        device_id=device_id,
                        started_at=tts_started,
                        sequence=max(0, tts_sequence[0] - 1),
                    )
                return PipelineResult(
                    session_id=session_id,
                    device_id=device_id,
                    character_id=self.character_id,
                    transcript=transcript,
                    emotion=device_emotion,
                    reply=reply.content,
                    audio=b"",
                    audio_format="pcm_s16le",
                )
        except StreamCancelled as exc:
            raise PipelineCancelled("session cancelled") from exc
        finally:
            self._unregister_session(session_id, cancel_event)

    def _stream_sentence_to_sink(
        self,
        sentence: str,
        cancel_event: threading.Event,
        loop: asyncio.AbstractEventLoop,
        send_audio: AudioSink | None,
        session_id: str,
        device_id: str,
        sequence: list[int],
        first_tts_chunk: list[bool],
        tts_started: float,
    ) -> None:
        synthesize_stream = getattr(self.tts_engine, "synthesize_stream", None)
        if not callable(synthesize_stream):
            raise RuntimeError("configured TTS engine does not support PCM streaming")
        for source_chunk in synthesize_stream(
            sentence, cancel_event=cancel_event
        ):
            self._check_cancel(cancel_event)
            if not source_chunk.pcm:
                continue
            chunk = AudioChunk(
                pcm=source_chunk.pcm,
                sample_rate=source_chunk.sample_rate,
                channels=source_chunk.channels,
                bits_per_sample=source_chunk.bits_per_sample,
                sequence=sequence[0],
                is_final=source_chunk.is_final,
            )
            if not first_tts_chunk[0]:
                first_tts_chunk[0] = True
                self.latency.record(
                    "tts_first_chunk",
                    session_id=session_id,
                    device_id=device_id,
                    started_at=tts_started,
                    sequence=chunk.sequence,
                )
            if send_audio is not None:
                future = asyncio.run_coroutine_threadsafe(send_audio(chunk), loop)
                future.result()
            sequence[0] += 1

    @staticmethod
    def _stream_silence_to_sink(
        frame_count: int,
        cancel_event: threading.Event,
        loop: asyncio.AbstractEventLoop,
        send_audio: AudioSink | None,
        sequence: list[int],
    ) -> None:
        if send_audio is None:
            sequence[0] += frame_count
            return
        for _ in range(frame_count):
            if cancel_event.is_set():
                raise PipelineCancelled("session cancelled")
            chunk = AudioChunk(
                pcm=bytes(640),
                sample_rate=16_000,
                channels=1,
                bits_per_sample=16,
                sequence=sequence[0],
            )
            future = asyncio.run_coroutine_threadsafe(send_audio(chunk), loop)
            future.result()
            sequence[0] += 1

    def _transcribe_wav_in_memory(self, wav_bytes: bytes) -> str:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            if (
                wav_file.getnchannels() != 1
                or wav_file.getsampwidth() != 2
                or wav_file.getframerate() != 16_000
            ):
                raise ValueError("ASR WAV must be 16 kHz signed 16-bit mono")
            pcm = wav_file.readframes(wav_file.getnframes())
        return self._transcribe_pcm(pcm, 16_000)

    def _transcribe_pcm(self, pcm: bytes, sample_rate: int) -> str:
        transcribe_pcm = getattr(self.asr_engine, "transcribe_pcm", None)
        if callable(transcribe_pcm):
            return transcribe_pcm(pcm, sample_rate)
        # Compatibility for third-party ASR engines that still only accept a
        # path. FasterWhisperEngine always takes the zero-copy branch above.
        wav_bytes = io.BytesIO()
        with wave.open(wav_bytes, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
        wav_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".wav", delete=False
            ) as wav_file:
                wav_file.write(wav_bytes.getvalue())
                wav_path = wav_file.name
            return self.asr_engine.transcribe(wav_path)
        finally:
            if wav_path:
                try:
                    os.unlink(wav_path)
                except OSError:
                    logger.warning("Unable to remove temporary WAV %s", wav_path)

    def preload(self) -> None:
        preload = getattr(self.asr_engine, "preload", None)
        if callable(preload):
            preload()

    def close(self) -> None:
        self.brain.close(wait=False)
