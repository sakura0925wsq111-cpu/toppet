"""HTTP client engines for the persistent local VoxCPM2 service."""

from __future__ import annotations

import audioop
import io
import logging
import threading
import time
import wave
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterator

import requests

from .base_tts import TTSEngine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioChunk:
    pcm: bytes
    sample_rate: int
    channels: int
    bits_per_sample: int
    sequence: int
    is_final: bool = False


@dataclass(frozen=True)
class VoxCPMStreamMetrics:
    inference_steps: int
    elapsed_seconds: float
    audio_seconds: float
    rtf: float


class IncrementalPCMResampler:
    """Stateful PCM converter; chunk boundaries never reset the rate phase."""

    def __init__(
        self,
        source_rate: int,
        *,
        source_channels: int = 1,
        bits_per_sample: int = 16,
        target_rate: int = 16_000,
    ) -> None:
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("sample rates must be positive")
        if source_channels not in (1, 2):
            raise ValueError("only mono or stereo PCM is supported")
        if bits_per_sample != 16:
            raise ValueError("only signed 16-bit PCM is supported")
        self.source_rate = source_rate
        self.source_channels = source_channels
        self.target_rate = target_rate
        self._width = bits_per_sample // 8
        self._frame_bytes = self._width * source_channels
        self._state = None
        self._remainder = b""

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        combined = self._remainder + data
        complete = len(combined) - (len(combined) % self._frame_bytes)
        frames = combined[:complete]
        self._remainder = combined[complete:]
        if final and self._remainder:
            raise RuntimeError("stream ended on a partial PCM frame")
        if not frames:
            return b""
        if self.source_channels == 2:
            frames = audioop.tomono(frames, self._width, 0.5, 0.5)
        if self.source_rate == self.target_rate:
            return frames
        converted, self._state = audioop.ratecv(
            frames,
            self._width,
            1,
            self.source_rate,
            self.target_rate,
            self._state,
        )
        return converted


class VoxCPMTTSEngine(TTSEngine):
    """Synthesize WAV bytes through a localhost-only VoxCPM2 service."""

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:9890",
        inference_steps: int = 4,
        timeout: float = 120.0,
        voice_id: str = "laoshaozi-personal",
        session: requests.Session | None = None,
        adaptive_steps: tuple[int, ...] | None = None,
        buffer_rtf_threshold: float = 0.95,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._inference_steps = inference_steps
        self._timeout = timeout
        self._voice_id = voice_id
        self._session = session or requests.Session()
        candidates = adaptive_steps or (inference_steps,)
        self._adaptive_steps = tuple(dict.fromkeys(int(step) for step in candidates))
        if not self._adaptive_steps or any(step < 1 for step in self._adaptive_steps):
            raise ValueError("adaptive_steps must contain positive integers")
        if inference_steps not in self._adaptive_steps:
            self._adaptive_steps = (inference_steps, *self._adaptive_steps)
        self._selected_steps = inference_steps
        self._buffer_rtf_threshold = float(buffer_rtf_threshold)
        self._metrics_lock = threading.Lock()
        self._metrics_by_step: dict[int, deque[float]] = defaultdict(
            lambda: deque(maxlen=3)
        )
        self._last_stream_metrics: VoxCPMStreamMetrics | None = None

    @property
    def engine_name(self) -> str:
        if len(self._adaptive_steps) == 1:
            return f"VoxCPM2 clone ({self._selected_steps} steps)"
        choices = "/".join(str(step) for step in self._adaptive_steps)
        return f"VoxCPM2 clone (adaptive {choices} steps)"

    @property
    def selected_steps(self) -> int:
        with self._metrics_lock:
            return self._selected_steps

    @property
    def last_stream_metrics(self) -> VoxCPMStreamMetrics | None:
        with self._metrics_lock:
            return self._last_stream_metrics

    @property
    def should_buffer_sentence(self) -> bool:
        metrics = self.last_stream_metrics
        return metrics is not None and metrics.rtf > self._buffer_rtf_threshold

    @property
    def voice_id(self) -> str:
        return self._voice_id

    @property
    def audio_format(self) -> str:
        return "wav"

    def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            raise ValueError("TTS text must not be blank")
        steps = self.selected_steps
        try:
            response = self._session.post(
                f"{self._api_url}/tts",
                json={"text": text, "inference_steps": steps},
                timeout=(3.0, self._timeout),
            )
        except requests.Timeout as exc:
            raise RuntimeError(f"VoxCPM2 TTS timeout ({self._timeout}s)") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"VoxCPM2 TTS unavailable: {exc}") from exc
        if response.status_code != 200:
            detail = response.text[:300]
            raise RuntimeError(
                f"VoxCPM2 TTS HTTP {response.status_code}: {detail}"
            )
        audio = response.content
        if not audio or not audio.startswith(b"RIFF"):
            raise RuntimeError("VoxCPM2 TTS returned invalid WAV audio")
        return audio

    def synthesize_stream(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
        *,
        streaming_prefix_len: int = 2,
    ) -> Iterator[AudioChunk]:
        text = text.strip()
        if not text:
            raise ValueError("TTS text must not be blank")
        steps = self.selected_steps
        started_at = time.perf_counter()
        output_bytes = 0
        completed = False
        try:
            response = self._session.post(
                f"{self._api_url}/tts/stream",
                json={
                    "text": text,
                    "inference_steps": steps,
                    "streaming_prefix_len": streaming_prefix_len,
                },
                timeout=(3.0, self._timeout),
                stream=True,
            )
        except requests.Timeout as exc:
            raise RuntimeError(f"VoxCPM2 TTS timeout ({self._timeout}s)") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"VoxCPM2 TTS unavailable: {exc}") from exc
        try:
            if response.status_code != 200:
                detail = response.text[:300]
                raise RuntimeError(
                    f"VoxCPM2 TTS HTTP {response.status_code}: {detail}"
                )
            headers = getattr(response, "headers", {})
            try:
                source_rate = int(headers.get("X-Sample-Rate", "48000"))
                channels = int(headers.get("X-Channels", "1"))
                bits = int(headers.get("X-Bits-Per-Sample", "16"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("VoxCPM2 TTS returned invalid PCM headers") from exc
            resampler = IncrementalPCMResampler(
                source_rate,
                source_channels=channels,
                bits_per_sample=bits,
            )
            pending: bytes | None = None
            sequence = 0
            for source_chunk in response.iter_content(chunk_size=4096):
                if cancel_event is not None and cancel_event.is_set():
                    return
                if not source_chunk:
                    continue
                pcm = resampler.feed(source_chunk)
                if not pcm:
                    continue
                if pending is not None:
                    output_bytes += len(pending)
                    yield AudioChunk(
                        pcm=pending,
                        sample_rate=16_000,
                        channels=1,
                        bits_per_sample=16,
                        sequence=sequence,
                    )
                    sequence += 1
                pending = pcm
            tail = resampler.feed(b"", final=True)
            if tail:
                if pending is not None:
                    output_bytes += len(pending)
                    yield AudioChunk(
                        pcm=pending,
                        sample_rate=16_000,
                        channels=1,
                        bits_per_sample=16,
                        sequence=sequence,
                    )
                    sequence += 1
                pending = tail
            if pending is not None and not (
                cancel_event is not None and cancel_event.is_set()
            ):
                output_bytes += len(pending)
                yield AudioChunk(
                    pcm=pending,
                    sample_rate=16_000,
                    channels=1,
                    bits_per_sample=16,
                    sequence=sequence,
                    is_final=True,
                )
            completed = True
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            if completed and output_bytes:
                self._record_stream_metrics(
                    steps,
                    elapsed_seconds=time.perf_counter() - started_at,
                    audio_seconds=output_bytes / (16_000 * 2),
                )

    def _record_stream_metrics(
        self, steps: int, *, elapsed_seconds: float, audio_seconds: float
    ) -> None:
        if audio_seconds <= 0:
            return
        rtf = elapsed_seconds / audio_seconds
        metrics = VoxCPMStreamMetrics(
            inference_steps=steps,
            elapsed_seconds=round(elapsed_seconds, 3),
            audio_seconds=round(audio_seconds, 3),
            rtf=round(rtf, 3),
        )
        with self._metrics_lock:
            self._last_stream_metrics = metrics
            self._metrics_by_step[steps].append(rtf)
            unprobed = [
                candidate
                for candidate in self._adaptive_steps
                if not self._metrics_by_step[candidate]
            ]
            if unprobed:
                self._selected_steps = unprobed[0]
            else:
                self._selected_steps = min(
                    self._adaptive_steps,
                    key=lambda candidate: sum(self._metrics_by_step[candidate])
                    / len(self._metrics_by_step[candidate]),
                )
        logger.info(
            "VoxCPM stream metrics steps=%d elapsed=%.3fs audio=%.3fs rtf=%.3f next_steps=%d buffer_next=%s",
            steps,
            elapsed_seconds,
            audio_seconds,
            rtf,
            self.selected_steps,
            rtf > self._buffer_rtf_threshold,
        )

    def is_ready(self, timeout: float = 1.0) -> bool:
        try:
            response = self._session.get(
                f"{self._api_url}/health", timeout=timeout
            )
            if response.status_code != 200:
                return False
            return response.json().get("status") == "ready"
        except (requests.RequestException, ValueError):
            return False


class FallbackTTSEngine(TTSEngine):
    """Use a secondary engine whenever the cloned voice request fails."""

    def __init__(self, primary: TTSEngine, fallback: TTSEngine) -> None:
        self.primary = primary
        self.fallback = fallback

    @property
    def engine_name(self) -> str:
        return f"{self.primary.engine_name} -> {self.fallback.engine_name}"

    @property
    def voice_id(self) -> str:
        return self.primary.voice_id

    @property
    def audio_format(self) -> str:
        # Both current engines produce file-encoded bytes. The fallback result
        # can differ by call, so synthesize records which format was returned.
        return self._last_format

    def synthesize(self, text: str) -> bytes:
        try:
            audio = self.primary.synthesize(text)
            self._last_format = self.primary.audio_format
            return audio
        except Exception as exc:
            logger.warning(
                "Primary TTS failed (%s); falling back to %s",
                exc,
                self.fallback.engine_name,
            )
            audio = self.fallback.synthesize(text)
            self._last_format = self.fallback.audio_format
            return audio

    def synthesize_stream(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[AudioChunk]:
        emitted = False
        primary_stream = getattr(self.primary, "synthesize_stream", None)
        if callable(primary_stream):
            try:
                for chunk in primary_stream(text, cancel_event=cancel_event):
                    emitted = True
                    self._last_format = "pcm_s16le"
                    yield chunk
                return
            except Exception:
                if emitted:
                    raise
                logger.exception("Primary streaming TTS failed before first chunk")
        if cancel_event is not None and cancel_event.is_set():
            return
        audio = self.fallback.synthesize(text)
        self._last_format = self.fallback.audio_format
        if self._last_format.lower() != "wav":
            raise RuntimeError(
                "streaming fallback must provide PCM WAV; encoded fallback is unsupported"
            )
        with wave.open(io.BytesIO(audio), "rb") as wav_file:
            source_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            width = wav_file.getsampwidth()
            frames = wav_file.readframes(wav_file.getnframes())
        resampler = IncrementalPCMResampler(
            source_rate,
            source_channels=channels,
            bits_per_sample=width * 8,
        )
        pcm = resampler.feed(frames, final=True)
        pieces = [pcm[offset:offset + 3200] for offset in range(0, len(pcm), 3200)]
        for sequence, piece in enumerate(pieces):
            if cancel_event is not None and cancel_event.is_set():
                return
            yield AudioChunk(
                pcm=piece,
                sample_rate=16_000,
                channels=1,
                bits_per_sample=16,
                sequence=sequence,
                is_final=sequence == len(pieces) - 1,
            )

    _last_format = "wav"
