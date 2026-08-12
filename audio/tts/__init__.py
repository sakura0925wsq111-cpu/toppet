# -*- coding: utf-8 -*-
"""TTS 子系统 —— 文字转语音"""

from .base_tts import TTSEngine
from .edge_tts_engine import EdgeTTSEngine, EDGE_VOICES, create_edge_tts
from .sovits_engine import SoVITSTTSEngine, create_sovits_tts
from .tts_manager import TTSManager, TTSWorker
from .voxcpm_engine import FallbackTTSEngine, VoxCPMTTSEngine

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
]
