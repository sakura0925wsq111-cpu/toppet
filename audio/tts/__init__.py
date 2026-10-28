# -*- coding: utf-8 -*-
"""TTS 子系统 —— 文字转语音"""

from .base_tts import TTSEngine
from .edge_tts_engine import EdgeTTSEngine, EDGE_VOICES, create_edge_tts
from .sovits_engine import SoVITSTTSEngine, create_sovits_tts
def __getattr__(name):
    if name in {"TTSManager", "TTSWorker"}:
        from . import tts_manager
        return getattr(tts_manager, name)
    raise AttributeError(name)
from .voxcpm_engine import (
    AudioChunk,
    FallbackTTSEngine,
    IncrementalPCMResampler,
    VoxCPMTTSEngine,
)

__all__ = [
    "TTSEngine",
    "EdgeTTSEngine",
    "EDGE_VOICES",
    "create_edge_tts",
    "SoVITSTTSEngine",
    "create_sovits_tts",
    "TTSManager",
    "TTSWorker",
    "VoxCPMTTSEngine",
    "FallbackTTSEngine",
    "AudioChunk",
    "IncrementalPCMResampler",
]
