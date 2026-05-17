# -*- coding: utf-8 -*-
"""AI 工作线程模块 - 后台调用大模型，不阻塞 UI。

核心职责：
1. 接收用户输入和历史记录，调用 DeepSeek API 生成回复
2. 本地处理时间/天气问题（不消耗 API 额度）
3. 解析模型返回的 JSON 回复

注意：此模块目前依赖 PyQt6.QtCore.QThread，未来解耦 UI 框架时可
替换为标准库 threading.Thread 或 asyncio。
"""

from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI
from PyQt6.QtCore import QThread, pyqtSignal

from core.emotion_definitions import (
    EMOTION_ALIASES,
    EMOTION_PROMPT,
    VALID_EMOTIONS,
)
from utils.weather import (
    is_weather_question,
    get_weather,
    get_weather_forecast,
    extract_weather_day_offset,
)
from utils.time_utils import (
    get_system_time,
    is_time_question,
)


class AIWorker(QThread):
    """后台大模型工作线程。

    为了防止请求网络时导致桌宠界面卡死，我们将 AI 思考的过程
    放在后台线程中执行。
    """

    # ---- 信号 ----
    response_ready = pyqtSignal(dict)  # 成功获取回复
    error_occurred = pyqtSignal(str)   # 发生报错

    def __init__(
        self,
        client: OpenAI | None,
        model: str,
        user_text: str,
        history: list[dict[str, str]],
        weather_city: str,
        parent=None,
    ):
        super().__init__(parent)
        self.client = client
        self.model = model
        self.user_text = user_text
        self.history = history
        self.weather_city = weather_city

    # ── 运行入口 ────────────────────────────────────────────────────

    def run(self):
        """线程主循环：先尝试本地处理（时间/天气），再调大模型。"""
        try:
            # 第一优先：本地确定性问题
            local_reply = self._build_local_reply()
            if local_reply:
                self.response_ready.emit(local_reply)
                return

            # 第二优先：调用大模型
            now_str = get_system_time()
            system_prompt = self._build_system_prompt(now_str)
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(self.history)
            messages.append({"role": "user", "content": self.user_text})

            self.response_ready.emit(self._call_ai(messages))
        except Exception as e:
            self.error_occurred.emit(str(e))

    # ── 本地回复 ────────────────────────────────────────────────────

    def _build_local_reply(self) -> dict[str, str] | None:
        """时间/天气属于确定性信息，优先本地处理，避免 API 报错或编造答案。"""
        # 天气问题
        if is_weather_question(self.user_text):
            day_offset = extract_weather_day_offset(self.user_text)
            if day_offset > 0:
                weather_text = get_weather_forecast(self.weather_city, day_offset)
            else:
                weather_text = get_weather(self.weather_city)
            return {"emotion": "NEUTRAL", "content": f"呆呆宝宝，{weather_text}"}

        # 时间问题
        if is_time_question(self.user_text):
            return {"emotion": "HAPPY", "content": f"呆呆宝宝，现在是 {get_system_time()}。"}

        return None

    # ── System Prompt ───────────────────────────────────────────────

    def _build_system_prompt(self, now_str: str) -> str:
        """构建核心性格设定书（System Prompt）。"""
        return f"""
你是一个桌面 AI 宠物，名字叫"老勺子"，是用户男友的电子虚拟版。
你的说话风格：亲昵、会吐槽、会护短，对用户的称呼："呆呆宝宝"、"呆呆"、"人机"，自称"老勺子"。

你必须严格输出 JSON，格式必须是：
{{"emotion": "...", "content": "..."}}

规则：
1) emotion 只能从下面这些英文标签里选，必须原样返回英文 key：
{EMOTION_PROMPT}
2) content 是你对用户说的话，简短自然，中文。
3) 如果聊到当前时间，可参考本机时间：{now_str}
4) 不要编造实时天气；实时天气问题会由程序直接查询处理。
5) 当用户问食物热量，优先用 EATING 或 THINKING，并提醒"仅供参考"。
6) 当用户累、压力大或崩溃时，优先用 TIRED、SAD、LISTENING 或 HEART。
7) 禁止输出任何额外文本（比如 Markdown、解释、前后缀）。
""".strip()

    # ── AI 调用 ────────────────────────────────────────────────────

    def _call_ai(self, messages: list[dict[str, str]]) -> dict[str, str]:
        """调用大模型。若 JSON 模式不被兼容接口支持，则自动降级重试一次。"""
        if self.client is None:
            return {
                "emotion": "SLEEPY",
                "content": "呆呆宝宝，我现在还没配置 DEEPSEEK_API_KEY，普通聊天先开不了；但问时间和天气我能直接答。",
            }

        request_args = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.9,
            "max_tokens": 260,
        }

        try:
            completion = self.client.chat.completions.create(
                **request_args,
                response_format={"type": "json_object"},
            )
        except Exception as first_error:
            # 有些 OpenAI 兼容接口不支持 response_format，此时去掉该参数重试一次
            err_text = str(first_error).lower()
            if "response_format" not in err_text and "json" not in err_text:
                raise
            completion = self.client.chat.completions.create(**request_args)

        text = completion.choices[0].message.content or "{}"
        # 如果模型返回的内容几乎是空的（空的 Markdown 代码块等），去掉 response_format 重试
        if not text.strip() or len(text.strip()) < 10:
            completion = self.client.chat.completions.create(**request_args)
            text = completion.choices[0].message.content or "{}"
        return self._normalize_reply(self._parse_json(text))

    # ── JSON 解析 ───────────────────────────────────────────────────

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """解析模型返回；即使模型包了 Markdown 代码块，也尽量提取 JSON。"""
        cleaned = (text or "").strip()
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        if not cleaned:
            return {"emotion": "NEUTRAL", "content": "呆呆宝宝，我刚刚没组织好语言，再问我一次。"}
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                return {"emotion": "NEUTRAL", "content": "呆呆宝宝，我刚刚没组织好语言，再问我一次。"}
            data = json.loads(match.group(0))
        if not isinstance(data, dict):
            raise ValueError("模型返回的 JSON 不是对象")
        return data

    @staticmethod
    def _normalize_reply(data: dict[str, Any]) -> dict[str, str]:
        """统一校验模型回复，防止情绪标签或正文缺失导致界面报错。"""
        raw_emotion = str(data.get("emotion", "NEUTRAL")).strip()
        emotion = EMOTION_ALIASES.get(
            raw_emotion,
            re.sub(r"[\s-]+", "_", raw_emotion.upper()),
        )
        if emotion not in VALID_EMOTIONS:
            emotion = "NEUTRAL"

        content = str(data.get("content", "")).strip()
        if not content:
            content = "呆呆宝宝，我刚刚没组织好语言，再问我一次。"
        return {"emotion": emotion, "content": content}