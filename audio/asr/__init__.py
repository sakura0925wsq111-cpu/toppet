# -*- coding: utf-8 -*-
"""ASR 子系统 —— 语音识别"""

from .base_asr import ASREngine
from .whisper_asr import FasterWhisperEngine
def __getattr__(name):
    if name in {"ASRManager", "WhisperWorker"}:
        from . import asr_manager
        return getattr(asr_manager, name)
    raise AttributeError(name)

__all__ = [
    "ASREngine",
    "FasterWhisperEngine",
    "ASRManager",
    "WhisperWorker",
]
