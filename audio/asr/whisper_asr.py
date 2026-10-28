# -*- coding: utf-8 -*-
"""
FasterWhisper 引擎 —— 本地语音识别

基于 faster-whisper 库（CTranslate2 加速），
在本地运行 OpenAI Whisper 模型推理。

优点：
    - 完全离线，无需 API Key
    - CPU 推理速度尚可（small 模型约实时速度的 2-3 倍）
    - 中文识别准确率高

缺点：
    - 首次运行需下载模型（约 500MB-1.5GB）
    - CPU 推理有延迟（2-5 秒）

模型选择：
    tiny     (~75MB)  最快，中文准确率较低
    base     (~145MB) 平衡速度与准确率
    small    (~500MB) 推荐，中文准确率高  ← 默认
    medium   (~1.5GB) 更高准确率
    large-v3 (~3GB)   最高准确率
"""

from __future__ import annotations

import os

import numpy as np

from common.paths import WHISPER_MODELS_DIR

from .base_asr import ASREngine


# =============================================================================
# FasterWhisper 引擎
# =============================================================================

class FasterWhisperEngine(ASREngine):
    """基于 faster-whisper 的本地语音识别引擎。

    用法：
        engine = FasterWhisperEngine(model_size="small")
        text = engine.transcribe("/tmp/recording.wav")
        print(text)  # "今天天气怎么样"
    """

    # Whisper 模型缓存目录（放在 assets 下，不污染项目根目录）
    DEFAULT_MODEL_DIR = os.fspath(WHISPER_MODELS_DIR)

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "zh",
    ):
        """初始化 FasterWhisper 引擎。

        Args:
            model_size:   模型大小 (tiny/base/small/medium/large-v3)
            device:       推理设备 (cpu/cuda)
            compute_type: 计算精度 (int8/float16/float32)
            language:     识别语言 (zh/en/auto)
        """
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language

        # 延迟加载模型（首次 transcribe 时才下载）
        self._model = None

    @property
    def engine_name(self) -> str:
        return f"FasterWhisper ({self._model_size})"

    def transcribe(self, audio_path: str) -> str:
        """将音频文件转为文字。

        Args:
            audio_path: WAV 文件路径

        Returns:
            识别出的中文文本
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"音频文件不存在: {audio_path}")

        # 延迟加载模型
        if self._model is None:
            self._load_model()

        return self._transcribe_source(audio_path)

    def transcribe_pcm(self, pcm: bytes | np.ndarray, sample_rate: int = 16_000) -> str:
        """Recognize in-memory 16 kHz mono PCM without a temporary WAV file."""
        if sample_rate != 16_000:
            raise ValueError("FasterWhisperEngine expects 16000 Hz PCM")
        if isinstance(pcm, bytes):
            if not pcm or len(pcm) % 2:
                raise ValueError("PCM must contain complete signed 16-bit samples")
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            audio *= 1.0 / 32768.0
        else:
            audio = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if not audio.size:
            return ""
        return self._transcribe_source(audio)

    def _transcribe_source(self, source: str | np.ndarray) -> str:
        if self._model is None:
            self._load_model()
        short = isinstance(source, np.ndarray) and source.size < 32000
        if short:
            # Push-to-talk clips can end immediately after a short syllable.
            # Supply decoder context with silence, not an additional VAD gate.
            source = np.pad(source, (1280, 5120))
        segments, _info = self._model.transcribe(
            source,
            language=self._language if self._language != "auto" else None,
            beam_size=3 if short else 1,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt="中文口语对话。专有词：QQ、大肥鱼、桌宠。",
            max_new_tokens=32 if short else 160,
            # GPIO18 already supplies an exact push-to-talk boundary. A
            # second VAD pass can erase quiet short Chinese syllables from the
            # board microphone and adds latency, so the hardware path decodes
            # the bounded array directly.
            vad_filter=False,
        )

        # 拼接所有识别片段
        text_parts = [seg.text.strip() for seg in segments if seg.text.strip()]
        return "".join(text_parts) if text_parts else ""

    def preload(self) -> None:
        """??????????????????? UI??

        ????????????????????????
        """
        if self._model is None:
            self._load_model()

    def _load_model(self) -> None:
        """下载并加载 Whisper 模型。

        首次运行会自动从 HuggingFace 下载模型文件。
        下载进度会打印到控制台。
        """
        from faster_whisper import WhisperModel

        print(f"[FasterWhisper] 加载模型 {self._model_size} ...")
        print(f"[FasterWhisper] 模型缓存目录: {self.DEFAULT_MODEL_DIR}")

        # 确保缓存目录存在
        os.makedirs(self.DEFAULT_MODEL_DIR, exist_ok=True)

        self._model = WhisperModel(
            self._model_size,
            device=self._device,
            compute_type=self._compute_type,
            download_root=self.DEFAULT_MODEL_DIR,
        )
        print(f"[FasterWhisper] 模型加载完成")
