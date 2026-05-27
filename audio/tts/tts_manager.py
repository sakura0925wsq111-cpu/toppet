# -*- coding: utf-8 -*-
"""
TTSManager —— TTS 调度管理器

DesktopPet 唯一需要调用的 TTS 入口。
内部自动处理：线程调度、合成、播放、中断。

换引擎时只需：manager.set_engine(SoVITSEngine())，
DesktopPet 一行代码都不需要改。
"""

from __future__ import annotations

import io
import tempfile
import os
import time

from PyQt6.QtCore import QThread, pyqtSignal, QObject

from .base_tts import TTSEngine
from .edge_tts_engine import EdgeTTSEngine


# =============================================================================
# TTSWorker —— 后台合成 & 播放线程
# =============================================================================

class TTSWorker(QThread):
    """在后台线程中完成"合成 → 播放"全流程。

    绝对不阻塞主线程（PyQt6 UI）。

    信号：
        playback_started: 开始播放时发射
        playback_finished: 播放完成时发射
        playback_error:    播放出错时发射（错误信息）
    """

    playback_started = pyqtSignal()
    playback_finished = pyqtSignal()
    playback_error = pyqtSignal(str)

    def __init__(
        self,
        engine: TTSEngine,
        text: str,
        parent=None,
    ):
        """初始化 TTS 工作线程。

        Args:
            engine: TTS 引擎实例（EdgeTTS / SoVITS 等）
            text:   要合成并播放的文字
            parent: Qt 父对象
        """
        super().__init__(parent)
        self._engine = engine
        self._text = text
        self._stop_flag = False

    def stop_playback(self) -> None:
        """从外部请求停止播放。"""
        self._stop_flag = True

    def run(self) -> None:
        """在工作线程中执行：合成 → 保存 → 播放。

        由 QThread.start() 触发。
        """
        try:
            # ── 步骤1：调用 TTS 引擎合成音频 ──
            audio_bytes = self._engine.synthesize(self._text)
            if self._stop_flag:
                return
            if not audio_bytes:
                self.playback_error.emit("TTS 合成为空")
                return

            # ── 步骤2：写入临时文件（pygame 需要文件路径）──
            temp_fd, temp_path = tempfile.mkstemp(suffix=f".{self._engine.audio_format}")
            try:
                os.write(temp_fd, audio_bytes)
                os.close(temp_fd)

                if self._stop_flag:
                    return

                # ── 步骤3：发射"开始播放"信号 ──
                self.playback_started.emit()

                # ── 步骤4：使用 pygame 播放音频 ──
                self._play_with_pygame(temp_path)

                if self._stop_flag:
                    return

                # ── 步骤5：发射"播放完成"信号 ──
                self.playback_finished.emit()

            finally:
                # 清理临时文件
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        except Exception as exc:
            self.playback_error.emit(str(exc))

    def _play_with_pygame(self, file_path: str) -> None:
        """使用 pygame.mixer 异步播放音频文件。

        注意：pygame.mixer 本身是异步的（play() 立即返回），
        但我们用循环等待播放结束，以便在完成后通知主线程。
        等待期间检查 _stop_flag 以支持中断。
        """
        import pygame

        # 初始化 pygame mixer（如果尚未初始化）
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=24000, size=-16, channels=2)
        except Exception:
            # 如果初始化失败，尝试默认参数
            pygame.mixer.init()

        try:
            pygame.mixer.music.load(file_path)
            pygame.mixer.music.play()

            # 等待播放完成（每 100ms 检查一次中断标志）
            while pygame.mixer.music.get_busy():
                if self._stop_flag:
                    pygame.mixer.music.stop()
                    break
                time.sleep(0.05)  # 50ms 轮询，响应快且不占 CPU

        except Exception as exc:
            raise RuntimeError(f"pygame 播放失败: {exc}") from exc


# =============================================================================
# TTSManager —— 对外统一接口（继承 QObject 以支持信号）
# =============================================================================

class TTSManager(QObject):
    """TTS 管理器 —— DesktopPet 唯一需要调用的 TTS 入口。

    职责：
    - 管理 TTS 引擎实例
    - 创建后台线程进行合成和播放
    - 支持运行时切换引擎
    - 支持中断当前播放

    用法：
        # 创建管理器（默认使用 EdgeTTS 晓晓女声）
        tts = TTSManager()

        # 连接信号到 UI
        tts.playback_started.connect(on_playback_started)
        tts.playback_finished.connect(on_playback_finished)

        # 说话
        tts.speak("呆呆宝宝，找我啥事？")

        # 停止
        tts.stop()

        # 换声音（运行时，不影响任何 UI 代码）
        tts.set_engine(EdgeTTSEngine(voice="zh-CN-YunxiNeural"))

        # 后期换成 SoVITS
        # from audio.tts.sovits_engine import SoVITSTTSEngine
        # tts.set_engine(SoVITSTTSEngine(model_path="..."))
    """

    # ── 信号（必须在类级别定义，QObject 子类的要求）──
    playback_started = pyqtSignal()
    playback_finished = pyqtSignal()
    playback_error = pyqtSignal(str)

    def __init__(self, engine: TTSEngine | None = None, parent=None):
        """初始化 TTS 管理器。

        Args:
            engine: TTS 引擎实例。如果为 None，默认使用 EdgeTTS(晓晓)。
            parent: Qt 父对象
        """
        super().__init__(parent)
        self._engine = engine if engine is not None else EdgeTTSEngine()
        self._current_worker: TTSWorker | None = None

    def set_engine(self, engine: TTSEngine) -> None:
        """运行时切换 TTS 引擎。

        后期换 SoVITS / CosyVoice 时，只需调用此方法，
        DesktopPet 完全不需要修改。

        Args:
            engine: 新的 TTS 引擎实例
        """
        self._engine = engine

    def speak(self, text: str) -> None:
        """异步合成并播放语音。

        调用后立即返回（不阻塞主线程），
        实际合成和播放在 QThread 中进行。
        通过 playback_started / playback_finished 信号通知 UI。

        Args:
            text: 要播放的文字
        """
        if not text or not text.strip():
            return

        # 如果有正在播放的，先停掉
        self.stop()

        # 创建新的工作线程
        self._current_worker = TTSWorker(self._engine, text)

        # 连接 TTSWorker 的信号到 TTSManager 的信号（桥接）
        self._current_worker.playback_started.connect(
            self.playback_started.emit
        )
        self._current_worker.playback_finished.connect(
            self.playback_finished.emit
        )
        self._current_worker.playback_error.connect(
            self.playback_error.emit
        )

        # 播放完成后清理 worker
        self._current_worker.finished.connect(self._on_worker_finished)

        # 启动线程
        self._current_worker.start()

    def stop(self) -> None:
        """立即停止当前正在播放的语音。"""
        if self._current_worker is not None:
            self._current_worker.stop_playback()

    def is_playing(self) -> bool:
        """检查是否正在播放语音。"""
        return (
            self._current_worker is not None
            and self._current_worker.isRunning()
        )

    @property
    def engine_name(self) -> str:
        """返回当前引擎名称。"""
        return self._engine.engine_name

    def _on_worker_finished(self) -> None:
        """Worker 线程结束后的清理。"""
        if self._current_worker is not None:
            self._current_worker.deleteLater()
            self._current_worker = None
