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

from importlib import import_module


def __getattr__(name):
    # Preserve desktop exports while allowing pure engines to run without Qt,
    # microphone drivers, or the desktop player being imported as side effects.
    if name in {"VoiceRecorder", "AudioPlayer"}:
        module = ".recorder" if name == "VoiceRecorder" else ".player"
    elif name in {"ASREngine", "FasterWhisperEngine", "ASRManager", "WhisperWorker"}:
        module = ".asr"
    elif name in __all__:
        module = ".tts"
    else:
        raise AttributeError(name)
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


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
