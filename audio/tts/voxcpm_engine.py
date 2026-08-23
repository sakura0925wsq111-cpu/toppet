"""HTTP client engines for the persistent local VoxCPM2 service."""

from __future__ import annotations

import audioop
import io
import logging
import threading
import wave
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
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._inference_steps = inference_steps
        self._timeout = timeout
        self._voice_id = voice_id
        self._session = session or requests.Session()

    @property
    def engine_name(self) -> str:
        return f"VoxCPM2 clone ({self._inference_steps} steps)"

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
        try:
            response = self._session.post(
                f"{self._api_url}/tts",
                json={"text": text, "inference_steps": self._inference_steps},
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
        try:
            response = self._session.post(
                f"{self._api_url}/tts/stream",
                json={
                    "text": text,
                    "inference_steps": self._inference_steps,
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
                yield AudioChunk(
                    pcm=pending,
                    sample_rate=16_000,
                    channels=1,
                    bits_per_sample=16,
                    sequence=sequence,
                    is_final=True,
                )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

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
