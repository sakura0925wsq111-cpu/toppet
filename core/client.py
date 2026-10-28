# -*- coding: utf-8 -*-
"""
AI 客户端 —— 与大模型 API 交互的纯逻辑层

本模块负责：
1. 调用 DeepSeek / OpenAI 兼容 API 进行对话
2. 查询实时天气（wttr.in）
3. 获取本机时间
4. 根据用户输入判断是否需要本地处理（天气/时间）

所有方法均为纯 Python 逻辑，不依赖 PyQt6。
可以在 QThread、asyncio、或任意 Python 环境中调用。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote

import requests
from openai import OpenAI

from .config import WEEKDAY_NAMES, WEATHER_KEYWORDS
from .dialogue_policy import is_weather_request, is_time_request

logger = logging.getLogger(__name__)

# 从 emotion_scheduler 导入情绪相关常量
# 注意：避免循环导入，只在需要时延迟导入


_ACTION_CUE_RE = re.compile(
    r"(?:"
    r"抱|抱住|拥抱|摸|摸头|拍|拍拍|揉|揉揉|亲|亲亲|蹭|"
    r"挥手|招手|点头|摇头|眨眼|闭眼|睁眼|歪头|抬头|低头|"
    r"叹气|叹了口气|笑|微笑|坏笑|偷笑|大笑|哭|流泪|脸红|"
    r"鼓掌|比心|叉腰|摊手|耸肩|挠头|托腮|转身|靠近|坐下|"
    r"站起|跳|转圈|伸懒腰|打哈欠|沉默|思考|假装|看着|望着|"
    r"hugs?|smiles?|laughs?|sighs?|nods?|waves?|cries?"
    r")",
    flags=re.IGNORECASE,
)
_FACTUAL_PARENTHETICAL_RE = re.compile(
    r"[0-9=+*/%℃]|(?:是指|也就是|即|例如|比如|因为|所以|但是|"
    r"或者|以及|包括|简称|版本|时间)"
)


def _is_parenthetical_action(text: str) -> bool:
    """Return whether short parenthetical text is an action/stage direction."""
    cue = text.strip()
    if not cue or len(cue) > 40 or "\n" in cue:
        return False
    if _FACTUAL_PARENTHETICAL_RE.search(cue):
        return False
    return _ACTION_CUE_RE.search(cue) is not None


class _ParentheticalActionFilter:
    """Remove action cues without exposing partial cues during streaming."""

    _CLOSERS = {"（": "）", "(": ")"}

    def __init__(self):
        self._pending = ""
        self._closer = ""
        self._depth = 0
        self._drop_following_space = False

    def feed(self, text: str) -> str:
        output: list[str] = []
        for char in text:
            if self._pending:
                self._pending += char
                if char == self._pending[0]:
                    self._depth += 1
                elif char == self._closer:
                    self._depth -= 1
                    if self._depth == 0:
                        candidate = self._pending
                        self._pending = ""
                        if _is_parenthetical_action(candidate[1:-1]):
                            self._drop_following_space = True
                        else:
                            output.append(candidate)
                elif len(self._pending) > 160:
                    output.append(self._pending)
                    self._pending = ""
                    self._depth = 0
                continue

            if char in self._CLOSERS:
                self._pending = char
                self._closer = self._CLOSERS[char]
                self._depth = 1
                continue

            if self._drop_following_space and char.isspace():
                continue
            self._drop_following_space = False
            output.append(char)
        return "".join(output)

    def finish(self) -> str:
        pending = self._pending
        self._pending = ""
        self._closer = ""
        self._depth = 0
        if pending and _is_parenthetical_action(pending[1:]):
            return ""
        return pending


def strip_parenthetical_actions(text: str) -> str:
    """Strip parenthesized action cues while preserving factual parentheses."""
    action_filter = _ParentheticalActionFilter()
    return action_filter.feed(text or "") + action_filter.finish()


# =============================================================================
# AI 回复的数据结构
# =============================================================================

@dataclass
class AIReply:
    """AI 回复的结构化数据。

    包含三个字段：
    - emotion: 情绪标签（如 HAPPY, SAD, NEUTRAL）
    - content: AI 回复的文本内容
    - is_local: 是否为本地处理（天气/时间），而非调用大模型
    """
    emotion: str
    content: str
    is_local: bool = False


class StreamCancelled(Exception):
    """Raised internally when a streaming request is intentionally cancelled."""


class StreamCancelToken:
    """Thread-safe cancellation event that also closes an attached HTTP stream."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._stream: Any | None = None

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()
        with self._lock:
            stream = self._stream
        self._close(stream)

    def attach(self, stream: Any) -> None:
        with self._lock:
            self._stream = stream
            cancelled = self._event.is_set()
        if cancelled:
            self._close(stream)

    def detach(self, stream: Any) -> None:
        with self._lock:
            if self._stream is stream:
                self._stream = None

    @staticmethod
    def _close(stream: Any | None) -> None:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("Failed to close cancelled LLM stream", exc_info=True)


class StreamingReplyParser:
    """Parse ``EMOTION\ncontent`` while preserving arbitrary token boundaries."""

    def __init__(
        self,
        on_emotion: Callable[[str], None] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ):
        self._on_emotion = on_emotion
        self._on_text_delta = on_text_delta
        self._header = ""
        self._header_done = False
        self.emotion = "NEUTRAL"
        self.content_parts: list[str] = []
        self._action_filter = _ParentheticalActionFilter()

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def has_content(self) -> bool:
        return bool(self.content.strip())

    def feed(self, delta: str) -> None:
        if not delta:
            return
        if self._header_done:
            self._emit_content(delta)
            return

        self._header += delta
        newline = self._header.find("\n")
        if newline < 0:
            return

        raw_header = self._header[:newline]
        remainder = self._header[newline + 1 :]
        self._header = ""
        self._finish_header(raw_header)
        if remainder:
            self._emit_content(remainder)

    def finish(self) -> None:
        if not self._header_done:
            # A non-conforming model may omit the emotion line. Treat its buffered
            # output as content rather than dropping a useful partial response.
            buffered = self._header
            self._header = ""
            raw = buffered.strip().strip("`#* ")
            normalized = AIClient.normalize_emotion(raw)
            if raw and (normalized != "NEUTRAL" or raw.upper() == "NEUTRAL"):
                self._finish_header(buffered)
            else:
                self._finish_header("NEUTRAL")
                if buffered:
                    self._emit_content(buffered)
        self._emit_filtered_content(self._action_filter.finish())

    def _finish_header(self, raw_header: str) -> None:
        cleaned = raw_header.strip().strip("`#* ")
        if ":" in cleaned or "：" in cleaned:
            cleaned = re.split(r"[:：]", cleaned, maxsplit=1)[-1].strip()
        self.emotion = AIClient.normalize_emotion(cleaned)
        self._header_done = True
        if self._on_emotion is not None:
            self._on_emotion(self.emotion)

    def _emit_content(self, text: str) -> None:
        self._emit_filtered_content(self._action_filter.feed(text))

    def _emit_filtered_content(self, text: str) -> None:
        if not text:
            return
        self.content_parts.append(text)
        if self._on_text_delta is not None:
            self._on_text_delta(text)


# =============================================================================
# AIClient —— 核心客户端
# =============================================================================

class AIClient:
    """AI 客户端 —— 封装所有对外 API 调用。

    纯 Python 类，不依赖任何 GUI 框架。
    可以安全地在任意线程中调用。

    用法：
        client = AIClient(api_key="sk-...", model="deepseek-chat")

        # 处理用户输入（自动判断天气/时间/对话）
        reply = client.process("今天上海天气怎么样", weather_city="上海")
        print(reply.emotion, reply.content)
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        temperature: float = 0.9,
        max_tokens: int = 260,
        user_address: str = "用户",
        request_timeout: float = 20.0,
    ):
        """初始化 AI 客户端。

        Args:
            api_key: DeepSeek / OpenAI 兼容 API Key
            base_url: API 端点地址
            model: 模型名称
            temperature: 生成温度（0-2，越高越随机）
            max_tokens: 单次回复最大 token 数
        """
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.user_address = user_address.strip() or "用户"

        # 如果没有 API Key，client 为 None
        # 此时只能处理本地查询（天气/时间），无法进行 AI 对话
        self._client: OpenAI | None = (
            OpenAI(api_key=api_key, base_url=base_url, timeout=request_timeout, max_retries=0)
            if api_key
            else None
        )

    # ═══════════════════════════════════════════════════════════════════
    # 主入口：处理用户输入
    # ═══════════════════════════════════════════════════════════════════

    def process(
        self,
        user_text: str,
        weather_city: str = "上海",
        conversation_history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> AIReply:
        """处理用户输入，返回 AI 回复。

        处理顺序：
        1. 先判断是否为天气/时间问题 → 本地处理
        2. 否则发送给大模型 API

        Args:
            user_text: 用户输入文本
            weather_city: 默认天气查询城市
            conversation_history: 历史对话消息列表
            system_prompt: 系统提示词（性格设定）

        Returns:
            AIReply 结构化回复
        """
        # ── 第一步：本地处理（天气/时间）──
        local_reply = self._build_local_reply(user_text, weather_city)
        if local_reply is not None:
            return local_reply

        # ── 第二步：调用大模型 API ──
        if self._client is None:
            return AIReply(
                emotion="SLEEPY",
                content=(
                    f"{self.user_address}，我现在还没配置 DEEPSEEK_API_KEY，"
                    "普通聊天先开不了；但问时间和天气我能直接答。"
                ),
                is_local=True,
            )

        # 构建消息列表
        messages = self._build_messages(user_text, conversation_history, system_prompt)

        # 调用 API 并解析
        result = self._request_ai_reply(messages)
        return AIReply(
            emotion=result["emotion"],
            content=result["content"],
            is_local=False,
        )

    def process_stream(
        self,
        user_text: str,
        weather_city: str = "上海",
        conversation_history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
        fallback_system_prompt: str | None = None,
        on_emotion: Callable[[str], None] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        cancel_event: threading.Event | StreamCancelToken | None = None,
        max_tokens: int | None = None,
    ) -> AIReply:
        """Stream an ``EMOTION\ncontent`` reply through lightweight callbacks.

        A failure before any useful content falls back to the established JSON
        request. Once content has been delivered, retrying would duplicate
        speech, so a mid-stream failure returns the useful partial response.
        """
        if cancel_event is not None and cancel_event.is_set():
            raise StreamCancelled()

        local_reply = self._build_local_reply(user_text, weather_city)
        if local_reply is not None or self._client is None:
            reply = local_reply or self.process(
                user_text,
                weather_city=weather_city,
                conversation_history=conversation_history,
                system_prompt=fallback_system_prompt or system_prompt,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelled()
            if on_emotion is not None:
                on_emotion(reply.emotion)
            if on_text_delta is not None:
                on_text_delta(reply.content)
            return reply

        messages = self._build_messages(
            user_text, conversation_history, system_prompt
        )
        parser = StreamingReplyParser(on_emotion, on_text_delta)
        started_at = time.monotonic()
        first_token_at: float | None = None

        stream = None
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
                stream=True,
            )
            attach = getattr(cancel_event, "attach", None)
            if callable(attach):
                attach(stream)
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    StreamCancelToken._close(stream)
                    raise StreamCancelled()
                delta = self._stream_delta_content(chunk)
                if not delta:
                    continue
                if first_token_at is None:
                    first_token_at = time.monotonic()
                    logger.info(
                        "voice_latency llm_first_token=%.3fs",
                        first_token_at - started_at,
                    )
                parser.feed(delta)
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelled()
            parser.finish()
            if not parser.has_content:
                logger.warning(
                    "LLM stream completed without content; falling back to JSON request"
                )
                fallback = self.process(
                    user_text,
                    weather_city=weather_city,
                    conversation_history=conversation_history,
                    system_prompt=fallback_system_prompt or system_prompt,
                )
                if cancel_event is not None and cancel_event.is_set():
                    raise StreamCancelled()
                if on_emotion is not None:
                    on_emotion(fallback.emotion)
                if on_text_delta is not None:
                    on_text_delta(fallback.content)
                return fallback
        except StreamCancelled:
            raise
        except Exception as exc:
            if getattr(exc, "status_code", None) in {401, 403, 429}:
                # A format fallback cannot repair credentials, permission or quota.
                raise
            if parser.has_content:
                parser.finish()
                logger.warning(
                    "LLM stream interrupted after useful content (%s); keeping partial reply", type(exc).__name__
                )
            else:
                logger.warning(
                    "LLM stream failed before content (%s); falling back to JSON request", type(exc).__name__
                )
                fallback = self.process(
                    user_text,
                    weather_city=weather_city,
                    conversation_history=conversation_history,
                    system_prompt=fallback_system_prompt or system_prompt,
                )
                if cancel_event is not None and cancel_event.is_set():
                    raise StreamCancelled()
                if on_emotion is not None:
                    on_emotion(fallback.emotion)
                if on_text_delta is not None:
                    on_text_delta(fallback.content)
                return fallback
        finally:
            detach = getattr(cancel_event, "detach", None)
            if callable(detach) and stream is not None:
                detach(stream)
            StreamCancelToken._close(stream)

        normalized = self.normalize_ai_reply(
            {"emotion": parser.emotion, "content": parser.content}
        )
        return AIReply(
            emotion=normalized["emotion"],
            content=normalized["content"],
            is_local=False,
        )

    # ═══════════════════════════════════════════════════════════════════
    # 本地处理：天气 + 时间
    # ═══════════════════════════════════════════════════════════════════

    def _build_local_reply(self, user_text: str, weather_city: str) -> AIReply | None:
        """尝试本地处理天气/时间问题。

        如果用户问的是实时天气或当前时间，直接查询后返回，
        不需要调用大模型（避免模型瞎编）。

        Returns:
            如果匹配到本地问题则返回 AIReply，否则返回 None。
        """
        # 判断是否问天气
        if self.is_weather_question(user_text):
            day_offset = self.extract_weather_day_offset(user_text)
            if day_offset > 0:
                weather_text = self.get_weather_forecast(weather_city, day_offset)
            else:
                weather_text = self.get_weather(weather_city)
            return AIReply(
                emotion="NEUTRAL",
                content=f"{self.user_address}，{weather_text}",
                is_local=True,
            )

        # 判断是否问时间
        if self.is_time_question(user_text):
            time_str = self.get_system_time()
            return AIReply(
                emotion="HAPPY",
                content=f"{self.user_address}，现在是 {time_str}。",
                is_local=True,
            )

        return None

    # ═══════════════════════════════════════════════════════════════════
    # 大模型 API 调用
    # ═══════════════════════════════════════════════════════════════════

    def _build_messages(
        self,
        user_text: str,
        conversation_history: list[dict[str, str]] | None,
        system_prompt: str | None,
    ) -> list[dict[str, str]]:
        """构建发送给大模型的消息列表。

        结构：[system_prompt] + [历史对话] + [当前用户输入]
        """
        messages: list[dict[str, str]] = []

        # 系统提示词（性格设定）
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # 历史对话
        if conversation_history:
            for message in conversation_history:
                copied = dict(message)
                if copied.get("role") == "assistant":
                    copied["content"] = strip_parenthetical_actions(
                        str(copied.get("content", ""))
                    ).strip()
                    if not copied["content"]:
                        continue
                messages.append(copied)

        # 当前用户输入
        messages.append({"role": "user", "content": user_text})

        return messages

    def _request_ai_reply(
        self, messages: list[dict[str, str]]
    ) -> dict[str, str]:
        """调用大模型 API，返回解析后的 {emotion, content}。

        如果 JSON 模式不被兼容接口支持，则自动降级重试一次。
        """
        request_args = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        try:
            # 优先使用 JSON 模式
            completion = self._client.chat.completions.create(
                **request_args,
                response_format={"type": "json_object"},
            )
        except Exception as first_error:
            # 部分兼容接口不支持 response_format，去掉后重试
            err_text = str(first_error).lower()
            if "response_format" not in err_text and "json" not in err_text:
                raise
            completion = self._client.chat.completions.create(**request_args)

        text = completion.choices[0].message.content or "{}"
        # 如果模型返回的内容几乎是空的（空的 Markdown 代码块等），去掉 response_format 重试
        if not text.strip() or len(text.strip()) < 10:
            completion = self._client.chat.completions.create(**request_args)
            text = completion.choices[0].message.content or "{}"
        return self.normalize_ai_reply(
            self.parse_json_reply(text, fallback_address=self.user_address),
            fallback_address=self.user_address,
        )

    @staticmethod
    def _stream_delta_content(chunk: Any) -> str:
        """Read a text delta from OpenAI-compatible object or dict chunks."""
        try:
            choices = chunk.choices
            if not choices:
                return ""
            return choices[0].delta.content or ""
        except AttributeError:
            choices = chunk.get("choices", []) if isinstance(chunk, dict) else []
            if not choices:
                return ""
            delta = choices[0].get("delta", {})
            return delta.get("content") or ""

    # ═══════════════════════════════════════════════════════════════════
    # 静态工具方法
    # ═══════════════════════════════════════════════════════════════════


    def chat_simple(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        raise_errors: bool = False,
    ) -> str:
        """Simple chat completion - direct API call returning plain text.

        Used by LongTermMemory.extract_from_conversation() and other callers
        that don't need JSON parsing or local weather/time handling.

        Args:
            messages: List of message dicts with role and content.
            temperature: Override temperature, None uses instance default.
            max_tokens: Override max tokens, None uses instance default.

        Returns:
            Raw text response from the model. By default failures return an
            empty string; durable background jobs can request exceptions.
        """
        if self._client is None:
            if raise_errors:
                raise RuntimeError("AI client is not configured")
            return ""

        try:
            request_client = self._client
            if timeout is not None:
                request_client = self._client.with_options(
                    timeout=timeout,
                    max_retries=0,
                )
            completion = request_client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            )
            return completion.choices[0].message.content or ""
        except Exception:
            if raise_errors:
                raise
            return ""

    @staticmethod
    def is_weather_question(text: str) -> bool:
        """判断用户是否在问天气。

        命中后直接走实时天气接口，不再让模型瞎猜。
        """
        return is_weather_request(text)

    @staticmethod
    def is_time_question(text: str) -> bool:
        """判断用户是否在问当前时间或日期。"""
        return is_time_request(text)

    @staticmethod
    def get_system_time() -> str:
        """获取当前本机时间，直接用于回答时间类问题。"""
        now = datetime.now()
        weekday = WEEKDAY_NAMES[now.weekday()]
        return f"{now:%Y年%m月%d日} {weekday} {now:%H:%M:%S}"

    @staticmethod
    def extract_weather_day_offset(text: str) -> int:
        """从用户文本中提取天气查询的天数偏移。

        Returns:
            0 = 今天, 1 = 明天, 2 = 后天
        """
        lowered = text.lower()
        if any(w in lowered for w in ("后天", "day after tomorrow")):
            return 2
        if any(w in lowered for w in ("明天", "明日", "tomorrow")):
            return 1
        return 0

    @staticmethod
    def extract_weather_city(text: str, default_city: str = "上海") -> str:
        """从用户输入中尝试提取城市名称。

        支持中文和英文城市名。提取不到时返回默认城市。
        """
        # 尝试匹配中文城市名
        cn_match = re.search(r"([一-龥]{2,12})(?:的)?(?:天气|气温|温度)", text)
        if cn_match:
            return AIClient._clean_city_name(cn_match.group(1)) or default_city

        # 尝试匹配英文城市名
        en_patterns = [
            r"(?:weather|temperature)\s+(?:in\s+)?([a-zA-Z][a-zA-Z\s-]{1,30})",
            r"(?:in|for)\s+([a-zA-Z][a-zA-Z\s-]{1,30})\s+(?:weather|temperature)",
        ]
        for pattern in en_patterns:
            en_match = re.search(pattern, text, flags=re.IGNORECASE)
            if en_match:
                city = en_match.group(1).strip(" ,.!?")
                if city.lower() not in {"today", "tomorrow", "now", "please"}:
                    return city

        return default_city

    @staticmethod
    def _clean_city_name(city: str) -> str | None:
        """清理城市名称中的口语前后缀。

        去掉"帮我查""今天"等前缀，避免把它们当成城市名。
        """
        city = city.strip(" ，。！？?的")
        prefixes = (
            "帮我查一下", "帮我查查", "帮我查", "查一下", "查询", "查查",
            "看看", "看下", "告诉我", "请问",
            "今天", "明天", "后天", "现在", "当前",
        )
        suffixes = (
            "今天", "明天", "后天", "现在", "当前",
            "这边", "那里", "哪里", "怎么样",
        )

        changed = True
        while changed:
            changed = False
            for prefix in prefixes:
                if city.startswith(prefix):
                    city = city[len(prefix):].strip(" ，。！？?的")
                    changed = True
            for suffix in suffixes:
                if city.endswith(suffix):
                    city = city[:-len(suffix)].strip(" ，。！？?的")
                    changed = True

        invalid_names = {"今天", "明天", "后天", "现在", "当前", "这里", "本地", "天气", "气温", "温度"}
        if not city or city in invalid_names:
            return None
        return city

    @staticmethod
    def parse_json_reply(
        text: str, fallback_address: str = "用户"
    ) -> dict[str, Any]:
        """解析模型返回的 JSON。

        即使模型包了 Markdown 代码块，也尽量提取 JSON。
        """
        cleaned = (text or "").strip()
        # 去除 Markdown 代码块标记
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()

        fallback = (
            f"{fallback_address.strip() or '用户'}，"
            "我刚刚没组织好语言，再问我一次。"
        )
        if not cleaned:
            return {"emotion": "NEUTRAL", "content": fallback}

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # 尝试从文本中提取第一个 JSON 对象
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                return {"emotion": "NEUTRAL", "content": fallback}
            data = json.loads(match.group(0))

        if not isinstance(data, dict):
            raise ValueError("模型返回的 JSON 不是对象")
        return data

    @staticmethod
    def normalize_ai_reply(
        data: dict[str, Any],
        emotion_aliases: dict[str, str] | None = None,
        valid_emotions: set[str] | None = None,
        fallback_address: str = "用户",
    ) -> dict[str, str]:
        """统一校验模型回复，防止情绪标签或正文缺失。

        延迟导入 emotion_resolver 常量以避免循环依赖。

        Args:
            data: 模型返回的原始字典
            emotion_aliases: 情绪别名映射（可选，运行时传入）
            valid_emotions: 合法情绪集合（可选，运行时传入）
        """
        # 延迟导入以避免循环依赖
        if emotion_aliases is None or valid_emotions is None:
            from systems.emotion_resolver import EMOTION_ALIASES, VALID_EMOTIONS
            if emotion_aliases is None:
                emotion_aliases = EMOTION_ALIASES
            if valid_emotions is None:
                valid_emotions = VALID_EMOTIONS

        raw_emotion = str(data.get("emotion", "NEUTRAL")).strip()
        emotion = emotion_aliases.get(
            raw_emotion,
            re.sub(r"[\s-]+", "_", raw_emotion.upper()),
        )
        if emotion not in valid_emotions:
            emotion = "NEUTRAL"

        content = strip_parenthetical_actions(
            str(data.get("content", ""))
        ).strip()
        if not content:
            content = (
                f"{fallback_address.strip() or '用户'}，"
                "我刚刚没组织好语言，再问我一次。"
            )

        return {"emotion": emotion, "content": content}

    @staticmethod
    def normalize_emotion(raw_emotion: str) -> str:
        """Normalize one streamed emotion label through the existing rules."""
        normalized = AIClient.normalize_ai_reply(
            {"emotion": raw_emotion, "content": "placeholder"}
        )
        return normalized["emotion"]

    # ═══════════════════════════════════════════════════════════════════
    # 天气 API
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _weather_description(current: dict[str, Any]) -> str:
        """从 wttr.in 的返回值中优先取中文天气描述。"""
        for key in ("lang_zh", "weatherDesc"):
            values = current.get(key) or []
            if values and values[0].get("value"):
                return str(values[0]["value"])
        return "天气情况未知"

    @staticmethod
    def get_weather(city: str) -> str:
        """查询实时天气。

        失败时说明原因，不再返回固定模拟数据。
        """
        city = (city or "").strip() or "上海"
        try:
            resp = requests.get(
                f"https://wttr.in/{quote(city)}",
                params={"format": "j1", "lang": "zh"},
                headers={"User-Agent": "laoshaozi-desktop-pet/1.0"},
                timeout=6,
            )
            resp.raise_for_status()
            payload = resp.json()
            current = payload.get("current_condition", [{}])[0]
            if not current:
                raise ValueError("天气服务返回空数据")

            desc = AIClient._weather_description(current)
            temp = current.get("temp_C")
            feels_like = current.get("FeelsLikeC")
            humidity = current.get("humidity")
            wind = current.get("windspeedKmph")

            parts = [f"{city}现在{desc}"]
            if temp not in (None, ""):
                parts.append(f"{temp}°C")
            if feels_like not in (None, ""):
                parts.append(f"体感 {feels_like}°C")
            if humidity not in (None, ""):
                parts.append(f"湿度 {humidity}%")
            if wind not in (None, ""):
                parts.append(f"风速 {wind} km/h")
            return "，".join(parts) + "。"
        except requests.Timeout:
            return f"暂时查不到 {city} 的实时天气：天气服务响应超时，稍后再试一下。"
        except requests.RequestException:
            return f"暂时查不到 {city} 的实时天气：网络或天气服务不可用。"
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            return f"暂时查不到 {city} 的实时天气：天气服务返回的数据格式异常。"

    @staticmethod
    def get_weather_forecast(city: str, day_offset: int) -> str:
        """查询天气预报（明天/后天）。

        使用 wttr.in 的 weather 数组。
        """
        city = (city or "").strip() or "上海"
        try:
            resp = requests.get(
                f"https://wttr.in/{quote(city)}",
                params={"format": "j1", "lang": "zh"},
                headers={"User-Agent": "laoshaozi-desktop-pet/1.0"},
                timeout=6,
            )
            resp.raise_for_status()
            payload = resp.json()
            weather_days = payload.get("weather", [])
            if len(weather_days) <= day_offset:
                raise ValueError("天气服务未返回该日预报")

            day = weather_days[day_offset]
            date_str = day.get("date", "")
            max_temp = day.get("maxtempC", "")
            min_temp = day.get("mintempC", "")

            # 取中午时段的天气描述作为当日代表
            hourly = day.get("hourly", [])
            mid_index = min(len(hourly) // 2, len(hourly) - 1) if hourly else 0
            desc = (
                AIClient._weather_description(hourly[mid_index])
                if hourly
                else "天气情况未知"
            )

            # 日期格式化
            try:
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                weekday = WEEKDAY_NAMES[date_obj.weekday()]
                date_label = f"{date_obj.month}月{date_obj.day}日 {weekday}"
            except (ValueError, TypeError):
                date_label = date_str

            day_labels = {0: "今天", 1: "明天", 2: "后天"}
            label = day_labels.get(day_offset, date_label)

            parts = [f"{city}{label}（{date_label}）{desc}"]
            if max_temp not in (None, ""):
                parts.append(f"最高 {max_temp}°C")
            if min_temp not in (None, ""):
                parts.append(f"最低 {min_temp}°C")
            return "，".join(parts) + "。"
        except requests.Timeout:
            return f"暂时查不到 {city} 的天气预报：天气服务响应超时，稍后再试一下。"
        except requests.RequestException:
            return f"暂时查不到 {city} 的天气预报：网络或天气服务不可用。"
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            return f"暂时查不到 {city} 的天气预报：天气服务返回的数据格式异常。"
