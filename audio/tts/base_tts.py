# -*- coding: utf-8 -*-
"""
TTS 抽象基类 —— 定义所有 TTS 引擎的统一接口

所有 TTS 后端（EdgeTTS / SoVITS / CosyVoice / FishSpeech）
都必须继承此类并实现 synthesize() 方法。

这样更换声音时，UI 层 / EmotionScheduler / AIWorker 都不需要任何修改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TTSEngine(ABC):
    """文字转语音引擎抽象基类。

    子类只需实现一个方法：synthesize(text) → bytes
    返回的音频数据可以是 MP3 / WAV / PCM 等格式，
    由 AudioPlayer 统一处理播放。

    后期计划支持的引擎：
        - EdgeTTSEngine       (edge-tts, 免费, 当前默认)
        - SoVITSTTSEngine     (GPT-SoVITS-v2pro, 本地推理, 高音质克隆)
        - CosyVoiceEngine     (阿里 CosyVoice, 本地推理, 情感控制)
        - FishSpeechEngine    (FishSpeech, 本地推理, 多语言)
        - OpenAITTSEngine     (OpenAI TTS API, 云端, 最高音质)
    """

    @abstractmethod
    def synthesize(self, text: str) -> bytes:
        """将文字合成为音频数据。

        Args:
            text: 要合成语音的文字（中文/英文）

        Returns:
            音频文件的二进制数据（MP3/WAV 等格式）

        Raises:
            Exception: 合成失败时抛出异常，由上层 TTSManager 捕获处理
        """
        ...

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """返回引擎名称（用于日志和调试）。"""
        ...

    @property
    def voice_id(self) -> str:
        """返回当前使用的声音 ID（子类可覆盖）。"""
        return "default"
