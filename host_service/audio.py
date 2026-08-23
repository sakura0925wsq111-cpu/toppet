"""Audio framing helpers for the TopPet host protocol."""

from __future__ import annotations

import io
import wave
import audioop


DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
DEFAULT_BITS_PER_SAMPLE = 16
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_AUDIO_SECONDS = 30


class AudioPayloadError(ValueError):
    """Raised when an ESP32 audio payload is not supported."""


def pcm_to_wav(
    pcm_bytes: bytes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = DEFAULT_CHANNELS,
    bits_per_sample: int = DEFAULT_BITS_PER_SAMPLE,
) -> bytes:
    """Wrap raw little-endian PCM in a canonical WAV container."""
    validate_audio_parameters(sample_rate, channels, bits_per_sample)
    if len(pcm_bytes) % (channels * bits_per_sample // 8) != 0:
        raise AudioPayloadError("PCM payload ends on a partial sample")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(bits_per_sample // 8)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return output.getvalue()


def normalize_audio_payload(
    payload: bytes,
    *,
    content_type: str = "",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = DEFAULT_CHANNELS,
    bits_per_sample: int = DEFAULT_BITS_PER_SAMPLE,
) -> bytes:
    """Return a WAV payload accepted by Faster Whisper.

    The entity firmware may send either a WAV file or raw PCM.  This first
    version intentionally rejects resampling/downmixing instead of silently
    changing the waveform; the hardware contract is 16 kHz, 16-bit, mono.
    """
    if not payload:
        raise AudioPayloadError("audio payload is empty")
    if len(payload) > MAX_AUDIO_BYTES:
        raise AudioPayloadError("audio payload is too large")

    is_wav = payload[:4] == b"RIFF" and payload[8:12] == b"WAVE"
    if is_wav or "wav" in content_type.lower():
        try:
            with wave.open(io.BytesIO(payload), "rb") as wav_file:
                actual_channels = wav_file.getnchannels()
                actual_width = wav_file.getsampwidth() * 8
                actual_rate = wav_file.getframerate()
                frame_count = wav_file.getnframes()
        except (wave.Error, EOFError) as exc:
            raise AudioPayloadError(f"invalid WAV: {exc}") from exc
        validate_audio_parameters(actual_rate, actual_channels, actual_width)
        if frame_count / actual_rate > MAX_AUDIO_SECONDS:
            raise AudioPayloadError("audio duration is too long")
        return payload

    validate_audio_parameters(sample_rate, channels, bits_per_sample)
    frame_width = channels * bits_per_sample // 8
    if len(payload) / frame_width / sample_rate > MAX_AUDIO_SECONDS:
        raise AudioPayloadError("audio duration is too long")
    return pcm_to_wav(
        payload,
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=bits_per_sample,
    )


def validate_audio_parameters(
    sample_rate: int,
    channels: int,
    bits_per_sample: int,
) -> None:
    if sample_rate != DEFAULT_SAMPLE_RATE:
        raise AudioPayloadError("only 16000 Hz audio is supported")
    if channels != DEFAULT_CHANNELS:
        raise AudioPayloadError("only mono audio is supported")
    if bits_per_sample != DEFAULT_BITS_PER_SAMPLE:
        raise AudioPayloadError("only 16-bit audio is supported")


def normalize_tts_wav(audio_bytes: bytes, audio_format: str) -> tuple[bytes, str]:
    """Convert PCM WAV TTS output to the ESP32 playback contract.

    VoxCPM2 may emit a model-dependent sample rate (often 48 kHz). The
    ESP32 playback path is deliberately fixed at 16 kHz/16-bit PCM, so the
    host performs the downmix/resample while it still has desktop-side audio
    tooling available. Encoded fallback formats are returned unchanged and
    remain unsupported by the current ESP32 player.
    """
    if audio_format.lower() != "wav":
        return audio_bytes, audio_format
    try:
        source = wave.open(io.BytesIO(audio_bytes), "rb")
    except (wave.Error, EOFError) as exc:
        raise AudioPayloadError(f"invalid TTS WAV: {exc}") from exc
    with source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        sample_rate = source.getframerate()
        frames = source.readframes(source.getnframes())
    if width != 2 or channels not in (1, 2):
        raise AudioPayloadError("TTS WAV must be signed 16-bit mono or stereo PCM")
    if channels == 2:
        frames = audioop.tomono(frames, width, 0.5, 0.5)
    if sample_rate != DEFAULT_SAMPLE_RATE:
        frames, _state = audioop.ratecv(
            frames, width, 1, sample_rate, DEFAULT_SAMPLE_RATE, None
        )
    return pcm_to_wav(frames), "wav"
