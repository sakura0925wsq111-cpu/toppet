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

from PyQt6.QtCore import QThread, pyqtSignal

from core.client import AIReply
from core.pet_brain import PetBrain


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

    def run(self) -> None:
        """在工作线程中执行（由 QThread.start() 触发）。

        调用 PetBrain.process() 处理用户输入，
        成功则发射 response_ready 信号，
        失败则发射 error_occurred 信号。
        """
        try:
            reply: AIReply = self.brain.process(self.user_text)
            self.response_ready.emit({
                "emotion": reply.emotion,
                "content": reply.content,
                "is_local": reply.is_local,
            })
        except Exception as exc:
            self.error_occurred.emit(str(exc))
