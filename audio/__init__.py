# -*- coding: utf-8 -*-
"""
Audio 模块 —— 语音输入输出

统一导出所有语音相关功能。
DesktopPet 只需 import 本模块即可使用全部语音能力。

子模块：
    tts/    — 文字转语音（EdgeTTS + 可插拔引擎）
    asr/    — 语音识别（FasterWhisper）
    recorder.py — 麦克风录音
    player.py   — 音频播放器
"""

# ── TTS ──
from .tts import (
    SoVITSTTSEngine,
    create_sovits_tts,
    TTSEngine,
    EdgeTTSEngine,
    EDGE_VOICES,
    create_edge_tts,
    TTSManager,
    TTSWorker,
    VoxCPMTTSEngine,
    FallbackTTSEngine,
)

# ── ASR ──
from .asr import (
    ASREngine,
    FasterWhisperEngine,
    ASRManager,
    WhisperWorker,
)

# ── 录音 & 播放 ──
from .recorder import VoiceRecorder
from .player import AudioPlayer


__all__ = [
    # TTS
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
    # ASR
    "ASREngine",
    "FasterWhisperEngine",
    "ASRManager",
    "WhisperWorker",
    # 录音 & 播放
    "VoiceRecorder",
    "AudioPlayer",
]
