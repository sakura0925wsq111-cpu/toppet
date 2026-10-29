"""Opt-in local diagnostic recordings; disabled in normal operation."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import wave


def capture_enabled() -> bool:
    return bool(os.getenv("TOPPET_AUDIO_CAPTURE_DIR", "").strip())


def _path(stage: str, key: str) -> Path | None:
    folder = os.getenv("TOPPET_AUDIO_CAPTURE_DIR", "").strip()
    if not folder:
        return None
    root = Path(folder)
    if not root.is_absolute() or root.resolve().drive.upper() == "C:":
        raise ValueError("Audio diagnostics require an absolute directory outside C:")
    if stage not in {"microphone", "tts-native", "tts-device"}:
        raise ValueError("Unknown audio capture stage")
    return root / (stage + "-" + hashlib.sha256(key.encode()).hexdigest()[:24])


def save_pcm(stage: str, key: str, pcm: bytes, *, sample_rate=16000, channels=1,
             **metadata) -> None:
    """A diagnostic disk failure must never break recording or playback."""
    try:
        path = _path(stage, key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path.with_suffix(".wav")), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm)
        save_metadata(stage, key, sample_rate=sample_rate, channels=channels,
                      pcm_bytes=len(pcm), **metadata)
    except (OSError, ValueError, wave.Error):
        logging.getLogger(__name__).warning("Audio diagnostic capture failed", exc_info=True)


def save_metadata(stage: str, key: str, **metadata) -> None:
    try:
        path = _path(stage, key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        target = path.with_suffix(".json")
        info = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        info.update(stage=stage, key=key, **metadata)
        target.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("Audio diagnostic metadata failed", exc_info=True)
