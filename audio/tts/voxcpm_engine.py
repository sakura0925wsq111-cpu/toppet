"""HTTP client engines for the persistent local VoxCPM2 service."""

from __future__ import annotations

import logging

import requests

from .base_tts import TTSEngine

logger = logging.getLogger(__name__)


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

    _last_format = "wav"
