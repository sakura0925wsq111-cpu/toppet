# -*- coding: utf-8 -*-
"""
语音录音器 —— 基于 pyaudio 的麦克风录音

提供"按住说话"式录音：
- 按下开始录音 → 松开停止录音 → 返回 WAV 文件路径
- 在 QThread 中运行，不阻塞 UI
"""

from __future__ import annotations

import os
import tempfile
import wave

from PyQt6.QtCore import QThread, pyqtSignal


# =============================================================================
# 录音参数
# =============================================================================

# 采样率（Hz）：16000 是 Whisper 模型推荐值
SAMPLE_RATE = 16000
# 采样位深：16bit
SAMPLE_WIDTH = 2
# 声道数：单声道
CHANNELS = 1
# 每次读取的帧数（1024 帧 ≈ 64ms）
CHUNK_SIZE = 1024
# pyaudio 格式常量
# 16bit = paInt16 = 8
FORMAT_INT16 = 8


# =============================================================================
# VoiceRecorder —— 后台录音线程
# =============================================================================

class VoiceRecorder(QThread):
    """后台录音线程。

    在子线程中持续录制麦克风音频，直到外部调用 stop()。
    停止后自动保存为 WAV 文件。

    用法：
        recorder = VoiceRecorder()
        recorder.recording_finished.connect(on_finished)  # on_finished(wav_path)
        recorder.recording_error.connect(on_error)        # on_error(msg)

        recorder.start()          # 开始录音
        # ... 用户按住说话 ...
        recorder.stop_recording() # 停止录音 → 保存 WAV → 发射信号
    """

    # 录音完成信号（参数：WAV 文件路径）
    recording_finished = pyqtSignal(str)
    # 录音出错信号
    recording_error = pyqtSignal(str)

    def __init__(self, sample_rate: int = SAMPLE_RATE, parent=None):
        """初始化录音器。

        Args:
            sample_rate: 采样率（Hz），默认 16000（Whisper 推荐）
            parent:      Qt 父对象
        """
        super().__init__(parent)
        self._sample_rate = sample_rate
        self._frames: list[bytes] = []
        self._recording = False

    def run(self) -> None:
        """在子线程中执行录音循环。

        由 QThread.start() 触发。
        持续录音直到 self._recording 变为 False。
        """
        try:
            import pyaudio

            audio = pyaudio.PyAudio()

            # 打开麦克风流
            stream = audio.open(
                format=FORMAT_INT16,
                channels=CHANNELS,
                rate=self._sample_rate,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
            )

            self._recording = True
            self._frames = []

            # 循环读取音频帧
            while self._recording:
                try:
                    data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                    self._frames.append(data)
                except Exception:
                    # 偶尔的溢出是正常的，继续录制
                    continue

            # 停止并关闭流
            stream.stop_stream()
            stream.close()
            audio.terminate()

            # 保存为 WAV 文件
            wav_path = self._save_wav()
            self.recording_finished.emit(wav_path)

        except ImportError:
            self.recording_error.emit(
                "缺少 pyaudio 库。请运行: pip install pyaudio"
            )
        except Exception as exc:
            self.recording_error.emit(str(exc))

    def stop_recording(self) -> None:
        """停止录音。

        调用后录音循环会退出，自动保存 WAV 文件并发射 recording_finished 信号。
        """
        self._recording = False

    def is_recording(self) -> bool:
        """检查是否正在录音。"""
        return self._recording and self.isRunning()

    def _save_wav(self) -> str:
        """将录制的音频帧保存为 WAV 文件。

        Returns:
            WAV 文件的完整路径
        """
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        try:
            import wave as wav_mod
        except ImportError:
            wav_mod = wave

        with wav_mod.open(path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(self._sample_rate)
            wf.writeframes(b"".join(self._frames))

        return path
