# -*- coding: utf-8 -*-
"""
音频播放器 —— 基于 pygame.mixer 的异步音频播放

提供简单的播放/停止/状态查询接口，
被 TTSManager 和其他需要播放音频的模块共用。
"""

from __future__ import annotations

import time
import threading


class AudioPlayer:
    """异步音频播放器。

    封装 pygame.mixer，提供安全的初始化和播放控制。
    线程安全：播放和停止可在不同线程中调用。

    用法：
        player = AudioPlayer()
        player.play_file("/tmp/speech.mp3", on_done=callback)
        player.stop()
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._initialized = False
        self._init_mixer()

    def _init_mixer(self) -> None:
        """初始化 pygame 音频混音器。

        如果初始化失败（如无音频设备），标记为未初始化，
        后续 play 调用会静默跳过。
        """
        try:
            import pygame
            # 检查是否已初始化
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=24000, size=-16, channels=2)
            self._initialized = True
        except Exception:
            self._initialized = False

    def play_file(self, file_path: str, on_done=None) -> bool:
        """异步播放音频文件。

        play() 调用后立即返回，不阻塞调用线程。
        如果需要等播放完成，调用方应自行轮询或使用 on_done 回调。

        Args:
            file_path: 音频文件路径（MP3/WAV 等）
            on_done:   播放完成后的回调函数（可选）

        Returns:
            True 如果成功开始播放，False 如果播放器未初始化
        """
        with self._lock:
            if not self._initialized:
                if on_done:
                    on_done()
                return False

            try:
                import pygame
                pygame.mixer.music.load(file_path)
                pygame.mixer.music.play()
                if on_done:
                    self._wait_for_done(on_done)
                return True
            except Exception:
                if on_done:
                    on_done()
                return False

    def play_bytes(self, audio_bytes: bytes, suffix: str = ".mp3", on_done=None) -> bool:
        """播放音频字节数据。

        先将 bytes 写入临时文件，再调用 play_file()。

        Args:
            audio_bytes: 音频数据
            suffix:     临时文件后缀（默认 .mp3）
            on_done:    播放完成回调

        Returns:
            True 如果成功开始播放
        """
        import tempfile
        import os

        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(fd, audio_bytes)
            os.close(fd)
            return self.play_file(path, on_done=lambda: self._cleanup(path, on_done))
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            if on_done:
                on_done()
            return False

    def stop(self) -> None:
        """立即停止当前播放。"""
        with self._lock:
            if not self._initialized:
                return
            try:
                import pygame
                pygame.mixer.music.stop()
            except Exception:
                pass

    def is_playing(self) -> bool:
        """检查是否正在播放。"""
        with self._lock:
            if not self._initialized:
                return False
            try:
                import pygame
                return pygame.mixer.music.get_busy()
            except Exception:
                return False

    def wait_until_done(self, check_interval: float = 0.05, stop_check=None) -> None:
        """阻塞等待直到播放完成或外部请求停止。

        用于在 QThread 中等待播放结束（配合 stop_flag 支持中断）。

        Args:
            check_interval: 轮询间隔（秒）
            stop_check:    可调用对象，返回 True 时立即停止等待并终止播放
        """
        while self.is_playing():
            if stop_check and stop_check():
                self.stop()
                break
            time.sleep(check_interval)

    def _wait_for_done(self, on_done) -> None:
        """在后台线程中等待播放完成并调用回调。"""
        def _waiter():
            self.wait_until_done()
            on_done()
        threading.Thread(target=_waiter, daemon=True).start()

    @staticmethod
    def _cleanup(path: str, on_done=None) -> None:
        """清理临时文件。"""
        import os
        try:
            os.unlink(path)
        except OSError:
            pass
        if on_done:
            on_done()
