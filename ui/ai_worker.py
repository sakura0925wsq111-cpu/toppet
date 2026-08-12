# -*- coding: utf-8 -*-
"""
AIWorker —— 后台工作线程

将 PetBrain.process() 的调用放到 QThread 中运行，
避免网络请求阻塞主线程导致桌面宠物界面卡死。

本文件是 core 层与 PyQt6 之间的"胶水层"：
- 接收 core.PetBrain（纯逻辑）
- 用 QThread 包装异步执行
- 通过 pyqtSignal 将结果传回主线程
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QThread, pyqtSignal

from core.client import AIReply, StreamCancelled, StreamCancelToken
from core.pet_brain import PetBrain
from core.sentence_chunker import SentenceChunker

logger = logging.getLogger(__name__)


class AIWorker(QThread):
    """后台 AI 工作线程。

    包装 PetBrain.process()，使其在子线程中运行，
    完成后通过信号通知主线程。

    用法：
        brain = PetBrain(config)
        worker = AIWorker(brain, "今天天气怎么样")
        worker.response_ready.connect(on_response)
        worker.error_occurred.connect(on_error)
        worker.start()
    """

    # ── 信号 ──
    emotion_ready = pyqtSignal(str)
    text_delta = pyqtSignal(str)
    sentence_ready = pyqtSignal(str)
    response_ready = pyqtSignal(dict)  # 成功获取回复 {"emotion": ..., "content": ...}
    error_occurred = pyqtSignal(str)   # 发生异常的错误信息

    def __init__(
        self,
        brain: PetBrain,
        user_text: str,
        parent=None,
    ):
        """初始化工作线程。

        Args:
            brain: 宠物大脑实例（已配置好的 PetBrain）
            user_text: 用户输入的文本
            parent: Qt 父对象
        """
        super().__init__(parent)
        self.brain = brain
        self.user_text = user_text
        self._cancel_event = StreamCancelToken()
        self._chunker = SentenceChunker()
        self._started_at = 0.0
        self._first_content_logged = False
        self._first_sentence_logged = False

    def cancel_request(self) -> None:
        """Cooperatively cancel network streaming and suppress stale events."""
        self._cancel_event.set()

    def run(self) -> None:
        """在工作线程中执行（由 QThread.start() 触发）。

        调用 PetBrain.process() 处理用户输入，
        成功则发射 response_ready 信号，
        失败则发射 error_occurred 信号。
        """
        self._started_at = time.monotonic()
        try:
            reply: AIReply = self.brain.process_stream(
                self.user_text,
                on_emotion=self._on_emotion,
                on_text_delta=self._on_text_delta,
                cancel_event=self._cancel_event,
            )
            if self._cancel_event.is_set():
                return
            for sentence in self._chunker.flush():
                self._emit_sentence(sentence)
            self.response_ready.emit({
                "emotion": reply.emotion,
                "content": reply.content,
                "is_local": reply.is_local,
            })
        except StreamCancelled:
            logger.info("AI stream cancelled")
        except Exception as exc:
            if not self._cancel_event.is_set():
                self.error_occurred.emit(str(exc))

    def _on_emotion(self, emotion: str) -> None:
        if not self._cancel_event.is_set():
            self.emotion_ready.emit(emotion)

    def _on_text_delta(self, delta: str) -> None:
        if self._cancel_event.is_set() or not delta:
            return
        if not self._first_content_logged and delta.strip():
            self._first_content_logged = True
            logger.info(
                "voice_latency llm_first_content=%.3fs",
                time.monotonic() - self._started_at,
            )
        self.text_delta.emit(delta)
        for sentence in self._chunker.feed(delta):
            self._emit_sentence(sentence)

    def _emit_sentence(self, sentence: str) -> None:
        if self._cancel_event.is_set() or not sentence.strip():
            return
        if not self._first_sentence_logged:
            self._first_sentence_logged = True
            logger.info(
                "voice_latency first_sentence=%.3fs",
                time.monotonic() - self._started_at,
            )
        self.sentence_ready.emit(sentence)
