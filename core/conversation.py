# -*- coding: utf-8 -*-
"""
对话记忆管理 —— 短期对话历史

管理用户与 AI 之间的多轮对话记忆。
采用滑动窗口策略：当对话轮数超过上限时，自动裁剪最早的记录。

注意：记忆仅存在于内存中，程序关闭后即消失。
如需持久化记忆，可在此模块中扩展文件/数据库写入逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConversationHistory:
    """对话历史管理器。

    用法：
        history = ConversationHistory(max_rounds=15)
        history.append_user("今天天气怎么样")
        history.append_assistant("呆呆宝宝，我去帮你查一下。")
        messages = history.to_messages()  # 传给 AI API
    """

    # 最多保留的对话轮数（一问一答算一轮）
    max_rounds: int = 15

    # 内部存储：列表中的每个元素是 {role, content}
    _messages: list[dict[str, str]] = field(default_factory=list)

    def append(self, role: str, content: str) -> None:
        """添加一条消息到记忆。

        Args:
            role: 角色，"user" 或 "assistant"
            content: 消息正文
        """
        self._messages.append({"role": role, "content": content})
        self._trim()

    def append_user(self, content: str) -> None:
        """添加用户消息（快捷方法）。"""
        self.append("user", content)

    def append_assistant(self, content: str) -> None:
        """添加 AI 回复（快捷方法）。"""
        self.append("assistant", content)

    def to_list(self) -> list[dict[str, str]]:
        """返回消息列表的副本（不暴露内部引用）。"""
        return list(self._messages)

    def clear(self) -> None:
        """清空所有对话记忆。"""
        self._messages.clear()

    def count(self) -> int:
        """返回当前消息总数。"""
        return len(self._messages)

    def round_count(self) -> int:
        """返回当前对话轮数（一问一答算一轮）。"""
        return len(self._messages) // 2

    def _trim(self) -> None:
        """裁剪超出上限的旧消息。

        按 max_rounds 计算消息上限（轮数 × 2），
        保留最新的消息，丢弃最早的。
        """
        max_messages = self.max_rounds * 2
        if len(self._messages) > max_messages:
            self._messages = self._messages[-max_messages:]
