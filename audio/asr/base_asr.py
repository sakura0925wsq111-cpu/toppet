# -*- coding: utf-8 -*-
"""
ASR 抽象基类 —— 定义所有语音识别引擎的统一接口

所有 ASR 后端（Whisper / Vosk / SenseVoice 等）
都必须继承此类并实现 transcribe() 方法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ASREngine(ABC):
    """语音识别引擎抽象基类。

    子类只需实现一个方法：transcribe(audio_path) → str

    后期可扩展的引擎：
        - FasterWhisperEngine  (当前默认, 本地推理)
        - OpenAISpeechEngine  (OpenAI Whisper API)
        - VoskEngine          (离线小模型)
        - SenseVoiceEngine    (阿里 FunASR)
    """

    @abstractmethod
    def transcribe(self, audio_path: str) -> str:
        """将音频文件转为文字。

        Args:
            audio_path: 音频文件路径（WAV/MP3 等）

        Returns:
            识别出的文字

        Raises:
            Exception: 识别失败时抛出
        """
        ...

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """返回引擎名称。"""
        ...
