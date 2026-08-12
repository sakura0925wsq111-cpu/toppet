# -*- coding: utf-8 -*-
"""
长期记忆管理 —— 针对用户个人信息的持久化存储

实现：
1. UserMemory 数据类 —— 用户档案
2. LongTermMemory 管理器 —— 读写 memory.json，提供 get_prompt_text() 注入 system prompt
3. extract_from_conversation() —— 通过 AI 从对话中提取用户信息

接入方式：
    PetBrain.__init__()     → self.long_memory = LongTermMemory()
    PetBrain.system_prompt  → 拼接 self.long_memory.get_prompt_text()
    PetBrain.process()      → 调用 self.long_memory.extract_from_conversation()
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import TYPE_CHECKING

from common.paths import LOGS_DIR

if TYPE_CHECKING:
    from .client import AIClient

logger = logging.getLogger(__name__)


# =============================================================================
# 数据模型
# =============================================================================

@dataclass
class UserMemory:
    """用户个人档案 —— 存储从对话中学到的信息。

    所有字段均为可选：空字符串/空列表表示尚未采集。
    """

    # 基本信息
    real_name: str = "陈泽熙"               # 她真正的名字
    nickname: str = "呆呆"                # 或者你叫她的小名

    # 重要日子 (MM-DD 格式)
    birthday: str = "01-05"               # 生日
    anniversary: str = "10-30"            # 纪念日

    # 喜好与习惯
    favorite_foods: list[str] = field(default_factory=list)     # 爱吃的
    dislike_foods: list[str] = field(default_factory=list)      # 不爱吃的
    hobbies: list[str] = field(default_factory=list)            # 兴趣爱好
    habits: list[str] = field(default_factory=list)             # 生活/性格习惯
    personality_traits: list[str] = field(default_factory=list) # 性格标签

    # 事件
    recent_events: list[dict] = field(default_factory=list)     # [{"date":"2026-06-10","summary":"..."}]
    inside_jokes: list[str] = field(default_factory=list)       # 你俩的内部梗

    # 元数据
    collection_round: int = 0          # 已采集轮数，10 轮后停止
    last_updated: str = ""             # 最后更新时间 (ISO 格式)

    def has_any(self) -> bool:
        """是否有任何有效数据（排除元字段）。"""
        for field_name in [
            "real_name", "nickname", "birthday", "anniversary",
            "favorite_foods", "dislike_foods", "hobbies", "habits",
            "personality_traits", "recent_events", "inside_jokes",
        ]:
            value = getattr(self, field_name)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, (list, tuple)) and len(value) > 0:
                return True
        return False


# =============================================================================
# 记忆管理器
# =============================================================================

class LongTermMemory:
    """长期记忆管理器。

    负责读写 memory.json，提供 prompt 文本注入 system prompt，
    通过 AI 从对话中提取有效用户信息。

    用法：
        memory = LongTermMemory()

        # 启动时：自动加载 memory.json

        # 生成 system prompt 段落
        prompt_text = memory.get_prompt_text()

        # 每次对话后提取
        memory.extract_from_conversation(user_text, ai_reply, client)
    """

    # 最多采集轮数（超过后不再调 AI 提取）
    MAX_COLLECTION_ROUNDS: int = 10

    # 提取时的 temperature（比对话低，要求精确）
    EXTRACT_TEMPERATURE: float = 0.3
    EXTRACT_MAX_TOKENS: int = 400

    def __init__(self, file_path: str | None = None):
        """初始化记忆管理器。

        Args:
            file_path: memory.json 存放路径。默认为 logs/memory.json。
        """
        if file_path is None:
            self.file_path = os.fspath(LOGS_DIR / "memory.json")
        else:
            self.file_path = file_path

        self._lock = threading.RLock()
        self.memory = self._load()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def _load(self) -> UserMemory:
        """从 JSON 加载记忆，文件不存在或格式异常时返回默认空实例。"""
        if not os.path.exists(self.file_path):
            return UserMemory()

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 取消扁平化（兼容旧版数据格式）
            memory = UserMemory()
            for key, default_value in asdict(memory).items():
                if key in data:
                    setattr(memory, key, data[key])
            return memory
        except (json.JSONDecodeError, TypeError, OSError):
            return UserMemory()

    def save(self) -> None:
        """将记忆写入 JSON 文件。"""
        with self._lock:
            self.memory.last_updated = datetime.now().isoformat()
            try:
                os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
                with open(self.file_path, "w", encoding="utf-8") as f:
                    data = asdict(self.memory)
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except OSError:
                logger.exception("Failed to save long-term memory")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def has_memory(self) -> bool:
        """是否有任何有效记忆。"""
        with self._lock:
            return self.memory.has_any()

    # ------------------------------------------------------------------
    # Prompt 生成
    # ------------------------------------------------------------------

    def get_prompt_text(self) -> str:
        """将 UserMemory 转换为自然语言，供注入 system prompt。

        Returns:
            一段中文描述，可以直接拼接到 system prompt 中。
            如果没有任何记忆，返回空字符串。
        """
        with self._lock:
            if not self.memory.has_any():
                return ""
            # Copy nested lists/dicts so the background extractor cannot mutate
            # values while this prompt is being assembled.
            m = UserMemory(**asdict(self.memory))
        lines: list[str] = ["关于和你对话的人，你知道以下信息（在对话中自然地提及，不要生硬地背书）："]

        # 基本信息
        if m.real_name.strip() or m.nickname.strip():
            name_part = m.real_name.strip()
            nick_part = m.nickname.strip()
            if name_part and nick_part:
                lines.append(f"- 她叫{name_part}，你叫她'{nick_part}'")
            elif nick_part:
                lines.append(f"- 你叫她'{nick_part}'")
            elif name_part:
                lines.append(f"- 她叫{name_part}")

        # 重要日子
        if m.birthday.strip():
            lines.append(f"- 她的生日是 {m.birthday}（提前祝福与庆祝）")
        if m.anniversary.strip():
            lines.append(f"- 你俩的纪念日是 {m.anniversary}")

        # 食物
        if m.favorite_foods:
            foods = "、".join(m.favorite_foods)
            lines.append(f"- 她爱吃：{foods}")
        if m.dislike_foods:
            foods = "、".join(m.dislike_foods)
            lines.append(f"- 她不爱吃：{foods}")

        # 兴趣和习惯
        if m.hobbies:
            h = "、".join(m.hobbies)
            lines.append(f"- 她的爱好：{h}")
        if m.habits:
            h = "、".join(m.habits)
            lines.append(f"- 她的习惯：{h}")

        # 性格
        if m.personality_traits:
            traits = "、".join(m.personality_traits)
            lines.append(f"- 她的性格特点：{traits}（当她有负面情绪时要多安慰）")

        # 最近事件
        if m.recent_events:
            events = "；".join(
                f"{e.get('date','')} {e.get('summary','')}"
                for e in m.recent_events[-5:]  # 最多取最近 5 条
            )
            lines.append(f"- 最近发生的事：{events}")

        # 内部梗
        if m.inside_jokes:
            jokes = "；".join(m.inside_jokes)
            lines.append(f"- 你俩的内部梗：{jokes}（可以偶尔玩梗，但不要每次都提）")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 记忆提取
    # ------------------------------------------------------------------

    def extract_from_conversation(
        self,
        user_text: str,
        ai_reply: str,
        ai_client: AIClient,
    ) -> None:
        """从本轮对话中提取有效用户信息，写入 memory.json。

        仅在 collection_round < MAX_COLLECTION_ROUNDS 时调用 AI，
        超过后跳过，避免额外成本。

        Args:
            user_text: 用户本轮输入
            ai_reply: AI 本轮回复
            ai_client: AIClient 实例，用于调用 chat_simple 提取信息
        """
        started_at = time.monotonic()
        with self._lock:
            if self.memory.collection_round >= self.MAX_COLLECTION_ROUNDS:
                return

            # Build a stable snapshot, then release the lock during network I/O.
            existing_json = json.dumps(
                {
                    k: v for k, v in asdict(self.memory).items()
                    if k not in ("collection_round", "last_updated")
                },
                ensure_ascii=False,
                indent=2,
            )

        system_msg = (
            "你是一个信息提取助手。根据用户和AI宠物的对话，提取关于用户的个人信息。"
            "只提取确定性信息，不要编造。如果没有新信息，输出空 JSON 对象\"{}\"。"
            "输出必须是合法的 JSON，不要包含任何解释文本。"
        )

        user_msg = (
            f"已有的用户档案：\n{existing_json}\n\n"
            f"本轮对话：\n"
            f"用户说：{user_text}\n"
            f"AI回复：{ai_reply}\n\n"
            f"请以 JSON 格式输出需要新增或更新的字段（只输出变化的字段，不要全量）。"
        )

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        # 调用 AI（需要 AIClient.chat_simple 方法）
        try:
            raw = ai_client.chat_simple(
                messages,
                temperature=self.EXTRACT_TEMPERATURE,
                max_tokens=self.EXTRACT_MAX_TOKENS,
                timeout=8.0,
            )
        except Exception:
            logger.exception("Background memory extraction failed")
            return

        if not raw or not raw.strip():
            return

        # 解析 AI 返回
        updates = self._parse_extraction(raw)
        if updates is None:
            return

        # 合并更新（updates 中的字段覆盖/追加）
        with self._lock:
            if self.memory.collection_round >= self.MAX_COLLECTION_ROUNDS:
                return
            self._merge_updates(updates)
            self.memory.collection_round += 1
            self.save()
        logger.info(
            "voice_latency memory_extraction_complete=%.3fs",
            time.monotonic() - started_at,
        )

    def _parse_extraction(self, raw: str) -> dict | None:
        """解析 AI 提取结果，兼容各种奇怪输出格式。"""
        import re as _re

        cleaned = raw.strip()

        # 去除 Markdown 代码块标记
        cleaned = _re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            cleaned,
            flags=_re.IGNORECASE | _re.DOTALL,
        ).strip()

        if not cleaned or cleaned == "{}":
            return None

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = _re.search(r"\{.*\}", cleaned, flags=_re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None

        if not isinstance(data, dict) or not data:
            return None
        return data

    def _merge_updates(self, updates: dict) -> None:
        """将 updates 中的新信息合并到 self.memory。

        字符串字段：补充（不覆盖已有值）
        列表字段：追加新项（自动去重）
        """
        m = self.memory

        # 字符串字段 —— 只补充空值
        for key in ["real_name", "nickname", "birthday", "anniversary"]:
            new_val = updates.get(key, "")
            if isinstance(new_val, str) and new_val.strip():
                existing = getattr(m, key, "")
                if not existing.strip():
                    setattr(m, key, new_val.strip())

        # 列表字段 —— 追加并去重
        for key in [
            "favorite_foods", "dislike_foods", "hobbies", "habits",
            "personality_traits", "recent_events", "inside_jokes",
        ]:
            new_vals = updates.get(key)
            if not isinstance(new_vals, list):
                continue
            existing: list = getattr(m, key, [])
            for item in new_vals:
                if isinstance(item, dict):
                    # recent_events 中 dict，比较 summary 去重
                    item_summary = item.get("summary", "")
                    if not any(
                        e.get("summary", "") == item_summary
                        for e in existing
                    ):
                        existing.append(item)
                elif isinstance(item, str) and item.strip() and item.strip() not in existing:
                    existing.append(item.strip())

            # 限制列表长度
            max_len = 20 if key == "inside_jokes" else 10
            if len(existing) > max_len:
                setattr(m, key, existing[-max_len:])

            # recent_events 按日期排序
            if key == "recent_events":
                existing.sort(
                    key=lambda e: e.get("date", "2000-01-01"),
                    reverse=True,
                )
