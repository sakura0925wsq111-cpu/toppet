# -*- coding: utf-8 -*-
"""
ASRManager —— 语音识别管理器

管理"录音 → 识别"全流程：
1. 调用 VoiceRecorder 录音
2. 调用 Whisper 引擎识别
3. 返回识别文本

所有耗时操作都在 QThread 中，不阻塞 UI。
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QThread, pyqtSignal, QObject

from .base_asr import ASREngine
from .whisper_asr import FasterWhisperEngine
from ..recorder import VoiceRecorder


# =============================================================================
# WhisperWorker —— 后台识别线程
# =============================================================================

class WhisperWorker(QThread):
    """在后台线程中运行 Whisper 语音识别。

    信号：
        recognition_done: 识别完成（参数: 识别文本）
        recognition_error: 识别出错（参数: 错误信息）
    """

    recognition_done = pyqtSignal(str)
    recognition_error = pyqtSignal(str)

    def __init__(
        self,
        engine: ASREngine,
        audio_path: str,
        parent=None,
    ):
        """初始化识别线程。

        Args:
            engine:     ASR 引擎实例
            audio_path: 要识别的音频文件路径
            parent:     Qt 父对象
        """
        super().__init__(parent)
        self._engine = engine
        self._audio_path = audio_path

    def run(self) -> None:
        """在子线程中执行语音识别。"""
        try:
            text = self._engine.transcribe(self._audio_path)
            self.recognition_done.emit(text)
        except Exception as exc:
            self.recognition_error.emit(str(exc))
        finally:
            # 识别完成后清理临时音频文件
            try:
                if os.path.exists(self._audio_path):
                    os.unlink(self._audio_path)
            except OSError:
                pass


# =============================================================================
# ASRManager —— 语音识别管理器
# =============================================================================


# =============================================================================
# PreloadWorker ?? ???????
# =============================================================================

class PreloadWorker(QThread):
    """????? ASR ??????????????????"""

    preload_done = pyqtSignal()
    preload_error = pyqtSignal(str)

    def __init__(self, engine: ASREngine, parent=None):
        super().__init__(parent)
        self._engine = engine

    def run(self) -> None:
        try:
            if hasattr(self._engine, 'preload'):
                self._engine.preload()
            self.preload_done.emit()
        except Exception as exc:
            self.preload_error.emit(str(exc))


class ASRManager(QObject):
    """语音识别管理器 —— 协调录音和识别流程。

    用法：
        asr = ASRManager()

        # 连接信号
        asr.recording_started.connect(on_recording_started)   # 开始录音
        asr.recognition_started.connect(on_recognition_started) # 开始识别
        asr.recognition_done.connect(on_text_ready)            # 识别完成(text)
        asr.error_occurred.connect(on_error)                   # 出错

        # 按住说话
        asr.start_recording()
        # ... 用户说话 ...
        asr.stop_recording()  # 松手后自动识别
    """

    # ── 信号 ──
    recording_started = pyqtSignal()      # 开始录音
    recording_finished = pyqtSignal()     # 录音结束
    recognition_started = pyqtSignal()    # 开始识别
    recognition_done = pyqtSignal(str)    # 识别完成（文本）
    error_occurred = pyqtSignal(str)      # 出错

    def __init__(
        self,
        engine: ASREngine | None = None,
        parent=None,
    ):
        """初始化 ASR 管理器。

        Args:
            engine: ASR 引擎实例。如果为 None，默认使用 FasterWhisper(small)。
            parent: Qt 父对象
        """
        super().__init__(parent)
        self._engine = engine if engine is not None else FasterWhisperEngine()
        self._recorder: VoiceRecorder | None = None
        self._is_recording = False

    def start_recording(self) -> None:
        """开始录音（非阻塞）。

        发射 recording_started 信号。
        """
        if self._is_recording:
            return

        self._is_recording = True
        self._recorder = VoiceRecorder()
        self._recorder.recording_finished.connect(self._on_recording_done)
        self._recorder.recording_error.connect(self._on_recording_error)
        self._recorder.start()
        self.recording_started.emit()

    def stop_recording(self) -> None:
        """停止录音（非阻塞）。

        停止录音后会自动触发 Whisper 识别。
        识别结果通过 recognition_done 信号返回。
        """
        if not self._is_recording or self._recorder is None:
            return

        self._is_recording = False
        self._recorder.stop_recording()
        self.recording_finished.emit()

    def cancel(self) -> None:
        """取消录音（不进行识别）。"""
        if self._recorder is not None and self._recorder.is_recording():
            self._recorder.stop_recording()
            # 断开信号，不触发识别
            self._recorder.recording_finished.disconnect()
        self._is_recording = False
        self._recorder = None

    def is_recording(self) -> bool:
        """检查是否正在录音。"""
        return self._is_recording

    def set_engine(self, engine: ASREngine) -> None:
        """运行时切换 ASR 引擎。"""
        self._engine = engine

    def preload(self) -> None:
        """????????? ASR ???

        ??????????????????????????
        ????????? model_ready ???
        """
        self._preloader = PreloadWorker(self._engine)
        self._preloader.preload_done.connect(self._on_preload_done)
        self._preloader.preload_error.connect(self._on_preload_error)
        self._preloader.start()

    def _on_preload_done(self) -> None:
        print("[ASR] ???????")

    def _on_preload_error(self, error_msg: str) -> None:
        print(f"[ASR] ???????: {error_msg}")

    @property
    def engine_name(self) -> str:
        return self._engine.engine_name

    # ── 内部回调 ──

    def _on_recording_done(self, wav_path: str) -> None:
        """录音完成 → 开始识别。"""
        self.recognition_started.emit()

        # 在后台线程中运行 Whisper 识别
        self._worker = WhisperWorker(self._engine, wav_path)
        self._worker.recognition_done.connect(self.recognition_done.emit)
        self._worker.recognition_error.connect(self.error_occurred.emit)
        self._worker.start()

    def _on_recording_error(self, error_msg: str) -> None:
        """录音出错。"""
        self._is_recording = False
        self.error_occurred.emit(f"录音失败: {error_msg}")
