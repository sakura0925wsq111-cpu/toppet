"""ASR -> brain -> TTS orchestration shared by HTTP and WebSocket APIs."""

from __future__ import annotations

import asyncio
import io
import itertools
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
from audio.tts.qwen_engine import QwenTTSEngine, resolve_tts_config
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
from .subtitle import build_response_ui_event, build_subtitle_event
from .playback_windows import (PlaybackWindowWriter, DEFAULT_WINDOW_BYTES,
                               DEFAULT_INITIAL_WINDOW_BYTES, validate_window_bytes)

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
        sentence_pause_frames: int = 4,
        safe_tts_prefix_bytes: int | None = None,
        playback_window_bytes: int | None = DEFAULT_WINDOW_BYTES,
        initial_playback_window_bytes: int | None = DEFAULT_INITIAL_WINDOW_BYTES,
    ) -> None:
        self.brain = brain
        self.asr_engine = asr_engine
        self.tts_engine = tts_engine
        self.character_id = character_id
        self.emotion_resolver = emotion_resolver or EmotionResolver()
        self.latency = latency or LatencyTracker()
        self.sentence_pause_frames = max(0, int(sentence_pause_frames))
        if safe_tts_prefix_bytes is not None and (
            isinstance(safe_tts_prefix_bytes, bool)
            or not isinstance(safe_tts_prefix_bytes, int)
            or safe_tts_prefix_bytes <= 0
            or safe_tts_prefix_bytes % 2
        ):
            raise ValueError("safe_tts_prefix_bytes must be a positive even integer or None")
        # Keep the old PCM-only prefix experiment separate from accurately
        # declared playback windows. playback_window_bytes=None is the legacy path.
        self.safe_tts_prefix_bytes = safe_tts_prefix_bytes
        if playback_window_bytes is not None:
            validate_window_bytes(playback_window_bytes)
            if initial_playback_window_bytes is not None:
                validate_window_bytes(initial_playback_window_bytes)
                if initial_playback_window_bytes < playback_window_bytes:
                    raise ValueError("initial playback unit must be at least the steady window size")
        self.playback_window_bytes = playback_window_bytes
        self.initial_playback_window_bytes = initial_playback_window_bytes
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
        tts_config = resolve_tts_config(profile)
        edge_voice_key = tts_config.get(
            "fallback_voice", tts_config.get("voice", "yunxi")
        )
        edge_voice = EDGE_VOICES.get(edge_voice_key, edge_voice_key)
        edge_engine = EdgeTTSEngine(voice=edge_voice)

        if tts_config.get("engine") == "qwen":
            tts_engine = QwenTTSEngine(
                api_url=tts_config.get("api_url", "http://127.0.0.1:9891"),
                timeout=float(tts_config.get("timeout_seconds", 180)),
                voice_id=str(tts_config.get("voice", "qwen-user-20261028")),
                runtime=tts_config.get("qwen_runtime", "baseline"),
            )
        elif tts_config.get("engine") == "voxcpm":
            primary = VoxCPMTTSEngine(
                api_url=tts_config.get("api_url", "http://127.0.0.1:9890"),
                inference_steps=int(tts_config.get("inference_steps", 4)),
                timeout=float(tts_config.get("timeout_seconds", 120)),
                voice_id=str(tts_config.get("voice", "laoshaozi-personal")),
                # Keep the configured voice baseline fixed during host playback;
                # candidate comparisons belong in the benchmark, not a user turn.
                adaptive_steps=(int(tts_config.get("inference_steps", 4)),),
                buffer_rtf_threshold=float(
                    tts_config.get("buffer_rtf_threshold", 0.95)
                ),
            )
            if bool(tts_config.get("allow_voice_fallback", True)):
                tts_engine: TTSEngine = FallbackTTSEngine(primary, edge_engine)
            else:
                tts_engine = primary
        else:
            tts_engine = edge_engine

        asr_engine = FasterWhisperEngine(
            model_size=asr_model,
            device=asr_device,
            compute_type=asr_compute_type,
        )
        fast_qwen = tts_config.get("engine") == "qwen" and tts_config.get("qwen_runtime") == "fast"
        default_window = 20480 if fast_qwen else DEFAULT_WINDOW_BYTES
        default_initial_window = 20480 if fast_qwen else DEFAULT_INITIAL_WINDOW_BYTES
        return cls(
            brain,
            asr_engine,
            tts_engine,
            character_id=character_id,
            sentence_pause_frames=int(tts_config.get("sentence_pause_frames", 4)),
            playback_window_bytes=tts_config.get("playback_window_bytes", default_window),
            initial_playback_window_bytes=tts_config.get("initial_playback_window_bytes", default_initial_window),
        )

    @property
    def asr_name(self) -> str:
        return self.asr_engine.engine_name

    @property
    def tts_name(self) -> str:
        return self.tts_engine.engine_name

    @property
    def requires_qwen(self) -> bool:
        return isinstance(self.tts_engine, QwenTTSEngine)

    @property
    def requires_voxcpm(self) -> bool:
        if isinstance(self.tts_engine, VoxCPMTTSEngine):
            return True
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
        if self.safe_tts_prefix_bytes is not None and emit is not None:
            raise ValueError(
                "safe-prefix requires a PCM-only sink without ready-length records; use playback windows for device events"
            )
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
                chunker = SentenceChunker(min_chars=4, max_chars=14, comma_chars=8)
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
                def on_first_audio(sequence):
                    first_tts_chunk[0] = True
                    self.latency.record("tts_first_chunk", session_id=session_id,
                                        device_id=device_id, started_at=tts_started, sequence=sequence)
                writer = PlaybackWindowWriter(
                    window_bytes=self.playback_window_bytes, emit=event, send_audio=send_audio,
                    check_cancel=lambda: self._check_cancel(cancel_event), on_first_audio=on_first_audio,
                    initial_window_bytes=self.initial_playback_window_bytes,
                ) if self.playback_window_bytes is not None and self.safe_tts_prefix_bytes is None else None
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
                        if writer is not None:
                            writer.begin_sentence(sentence_text, str(callback_state["emotion"] or "NEUTRAL"))
                            await asyncio.to_thread(self._stream_sentence_to_writer,
                                                    sentence_text, cancel_event, loop, writer)
                            # Preserve the existing configured pauses; do not add
                            # silence to conceal transport starvation.
                            for _ in range(self.sentence_pause_frames):
                                await writer.write(bytes(640))
                            writer.end_sentence(sentence_text)
                            continue
                        sentence_start_sequence = tts_sequence[0]
                        await event(
                            "tts_sentence_start",
                            text=sentence_text,
                            sequence=sentence_start_sequence,
                        )
                        await event(
                            "response_ui",
                            **build_response_ui_event(
                                sentence_text,
                                str(callback_state["emotion"] or "NEUTRAL"),
                                sentence_start_sequence,
                            ),
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
                            (lambda byte_count: asyncio.run_coroutine_threadsafe(
                                event(
                                    "tts_sentence_ready",
                                    sequence=sentence_start_sequence,
                                    bytes=byte_count,
                                ),
                                loop,
                            ).result()) if emit is not None else None,
                        )
                        # Preserve a natural punctuation pause while the
                        # single-GPU VoxCPM service starts the next sentence.
                        # These zero samples are real stream continuity, not a
                        # prompt or prerecorded speech, and prevent the device
                        # ring from hard-underrunning between model requests.
                        await asyncio.to_thread(
                            self._stream_silence_to_sink,
                            self.sentence_pause_frames,
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
                    if writer is not None:
                        await writer.finish()
                        tts_sequence[0] = writer.sequence
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
        sentence_ready: Callable[[int], None] | None = None,
    ) -> None:
        synthesize_stream = getattr(self.tts_engine, "synthesize_stream", None)
        if not callable(synthesize_stream):
            raise RuntimeError("configured TTS engine does not support PCM streaming")
        if self.safe_tts_prefix_bytes is not None and sentence_ready is not None:
            raise ValueError("a prefix cannot provide a complete-sentence byte manifest")
        self._check_cancel(cancel_event)
        source = iter(synthesize_stream(sentence, cancel_event=cancel_event))
        try:
            buffered: list[AudioChunk] = []
            buffered_bytes = 0
            while self.safe_tts_prefix_bytes is None or buffered_bytes < self.safe_tts_prefix_bytes:
                self._check_cancel(cancel_event)
                try:
                    chunk = next(source)
                except StopIteration:
                    break
                if chunk.pcm:
                    buffered.append(chunk)
                    buffered_bytes += len(chunk.pcm)
            if not buffered:
                raise RuntimeError("TTS stream returned no PCM")
            self._check_cancel(cancel_event)
            if sentence_ready is not None:
                sentence_ready(buffered_bytes)
            logger.info(
                "TTS buffering policy=%s bytes=%d chunks=%d",
                "complete_sentence" if self.safe_tts_prefix_bytes is None else "bounded_prefix",
                buffered_bytes, len(buffered),
            )
            ordered = itertools.chain(buffered, source)
            while True:
                # Check before requesting the next block, including after sink cancellation.
                self._check_cancel(cancel_event)
                try:
                    source_chunk = next(ordered)
                except StopIteration:
                    break
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
                        "tts_first_chunk", session_id=session_id, device_id=device_id,
                        started_at=tts_started, sequence=chunk.sequence,
                    )
                if send_audio is not None:
                    asyncio.run_coroutine_threadsafe(send_audio(chunk), loop).result()
                sequence[0] += 1
        finally:
            close = getattr(source, "close", None)
            if callable(close):
                close()

    def _stream_sentence_to_writer(self, sentence, cancel_event, loop, writer):
        synthesize_stream = getattr(self.tts_engine, "synthesize_stream", None)
        if not callable(synthesize_stream):
            raise RuntimeError("configured TTS engine does not support PCM streaming")
        self._check_cancel(cancel_event)
        source = iter(synthesize_stream(sentence, cancel_event=cancel_event))
        seen = False
        expected_sequence = 0
        try:
            while True:
                self._check_cancel(cancel_event)
                try:
                    chunk = next(source)
                except StopIteration:
                    break
                if not chunk.pcm:
                    continue
                if ((chunk.sample_rate, chunk.channels, chunk.bits_per_sample) != (16000, 1, 16)
                        or len(chunk.pcm) % 2 or chunk.sequence != expected_sequence):
                    raise ValueError("invalid TTS PCM format or sequence")
                expected_sequence += 1
                seen = True
                asyncio.run_coroutine_threadsafe(writer.write(chunk.pcm), loop).result()
            if not seen:
                raise RuntimeError("TTS stream returned no PCM")
        finally:
            close = getattr(source, "close", None)
            if callable(close):
                close()

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
