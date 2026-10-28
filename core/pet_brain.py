# -*- coding: utf-8 -*-
"""
PetBrain —— 宠物交互中枢

这是整个宠物的"大脑"，负责协调：
1. 对话记忆（ConversationHistory）
2. API 调用（AIClient）
3. 性格设定（System Prompt）

对 UI 层只暴露一个接口：process(user_text) → AIReply。
UI 层不需要关心内部如何调度、如何拼接 prompt、如何管理记忆。

未来扩展语音时，只需要在此处增加音频相关的方法即可：
    - process_voice(audio_bytes) → AIReply
    - speak(text) → audio_bytes
    - interrupt() → 打断当前 AI 生成的回复
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from common.paths import LOGS_DIR

from .config import PetConfig
from .conversation import ConversationHistory
from .client import AIClient, AIReply
from .long_term_memory import LongTermMemory

# 延迟导入，避免循环依赖
from systems.emotion_resolver import EMOTION_PROMPT
from systems.character_manager import CharacterManager, DEFAULT_CHARACTER

logger = logging.getLogger(__name__)


class PetBrain:
    """宠物大脑 —— 交互中枢。

    纯 Python 类，不依赖任何 GUI 框架。

    用法：
        config = PetConfig()
        brain = PetBrain(config)

        reply = brain.process("今天上海天气怎么样")
        print(reply.emotion)  # "NEUTRAL"
        print(reply.content)  # "呆呆宝宝，上海现在晴天，22°C..."
    """

    def __init__(
        self,
        config: PetConfig | None = None,
        character: str = DEFAULT_CHARACTER,
    ):
        """初始化大脑。

        Args:
            config: 宠物配置。如果为 None，使用默认配置。
            character: 固定运行的人物模式。老勺子入口始终传入 personal。
        """
        if config is None:
            config = PetConfig()
        self.config = config

        # ── 角色包与 AI 客户端 ──
        self._char_manager = CharacterManager(current_character=character)
        self._current_character = character
        initial_profile = self._char_manager.get_profile(character)
        self.user_address = str(
            initial_profile.get("user_address", "用户")
        ).strip() or "用户"
        self.client = AIClient(
            api_key=config.api_key,
            base_url=config.api_base_url,
            model=config.model,
            temperature=config.ai_temperature,
            max_tokens=config.ai_max_tokens,
            user_address=self.user_address,
        )

        # ── 创建对话记忆 ──
        self.history = ConversationHistory(
            max_rounds=config.max_conversation_rounds,
        )

        # ── 当前角色的独立长期记忆 ──
        self._memories: dict[str, LongTermMemory] = {}
        self._get_or_create_memory(character)
        self._memory_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="long-term-memory",
        )
        self._closed = False

        # ── 默认城市 ──
        self.default_city = config.default_city


    # ═══════════════════════════════════════════════════════════════════
    # 性格设定（System Prompt）
    # ═══════════════════════════════════════════════════════════════════

    @property
    def current_character(self) -> str:
        return self._current_character

    @property
    def character_manager(self) -> CharacterManager:
        """Return the manager shared with the UI for this running mode."""
        return self._char_manager

    @current_character.setter
    def current_character(self, char_id: str) -> None:
        if char_id == self._current_character:
            return
        if not self._char_manager.set_character(char_id):
            raise ValueError(f"Unknown character: {char_id}")
        self._current_character = char_id
        profile = self._char_manager.get_profile(char_id)
        self.user_address = str(
            profile.get("user_address", "用户")
        ).strip() or "用户"
        self.client.user_address = self.user_address
        self._get_or_create_memory(char_id)

    def _get_or_create_memory(self, char_id: str) -> LongTermMemory:
        if char_id not in self._memories:
            profile = self._char_manager.get_profile(char_id)
            memory_file = profile.get("memory_file", f"memory_{char_id}.json")
            mem_path = str(LOGS_DIR / memory_file)
            starter_memory = self._char_manager.get_pack_file(
                profile.get("starter_memory_file"), char_id
            )
            self._memories[char_id] = LongTermMemory(
                file_path=mem_path,
                starter_memory_path=starter_memory,
            )
        return self._memories[char_id]

    def _get_memory(self) -> LongTermMemory:
        return self._get_or_create_memory(self._current_character)

    @property
    def system_prompt(self) -> str:
        return self._build_system_prompt(streaming=False)

    @property
    def stream_system_prompt(self) -> str:
        return self._build_system_prompt(streaming=True)

    def _build_system_prompt(self, streaming: bool) -> str:
        now_str = AIClient.get_system_time()
        profile = self._char_manager.get_profile(self._current_character)
        persona = profile.get("persona", "")
        if not persona:
            display_name = profile.get("display_name", self._current_character)
            persona = f"你是一个桌面 AI 宠物，名字叫“{display_name}”。"
        memory_text = self._get_memory().get_prompt_text()
        example = f"{self.user_address}，今天心情不错呀。"
        output_rule = (
            "你必须严格按以下流式格式输出：第一行只输出一个 emotion 英文标签，"
            "紧接一个换行；从第二行开始只输出对用户说的中文正文。不要输出 JSON、"
            f"Markdown、字段名或解释。示例：\nHAPPY\n{example}"
            if streaming
            else "你必须严格输出 JSON，格式必须是：\n"
            '{"emotion": "...", "content": "..."}'
        )
        return f"""{persona}
{memory_text}

{output_rule}

规则：
1) emotion 只能从下面这些英文标签里选，必须原样返回英文 key：
{EMOTION_PROMPT}
   除非回复确实完全没有情绪，否则不要默认选择 NEUTRAL；问候、安慰、喜欢、
   惊讶、开心、疲惫、工作、学习等场景应优先选择对应的具体标签。
2) content 是你对用户说的话，简短自然，中文。
3) 如果聊到当前时间，可参考本机时间：{now_str}
4) 不要编造实时天气；实时天气问题会由程序直接查询处理。
5) 当用户问食物热量，优先用 EATING 或 THINKING。
6) 当用户累、压力大或崩溃时，优先用 TIRED、SAD、LISTENING 或 HEART。
7) 禁止输出任何额外文本（比如 Markdown、解释、前后缀）。
8) 禁止用任何括号描述肢体动作、表情或舞台提示，只输出实际要说的话。
""".strip()

    def process(self, user_text: str) -> AIReply:
        """处理用户输入，返回 AI 回复。

        这是 UI 层唯一需要调用的方法。
        内部自动完成：
        1. 提取城市名称（用于天气查询）
        2. 拼接历史对话 + 系统提示词
        3. 调用 AI API 或本地查询
        4. 更新对话记忆

        Args:
            user_text: 用户输入的文本

        Returns:
            AIReply 结构化回复（emotion + content + is_local）
        """
        # 提取城市名称（如果用户提到了）
        city = AIClient.extract_weather_city(user_text, self.default_city)

        # 调用 AI 客户端处理
        reply = self.client.process(
            user_text=user_text,
            weather_city=city,
            conversation_history=self.history.to_list(),
            system_prompt=self.system_prompt,
        )

        # 更新对话记忆
        self.history.append_user(user_text)
        self.history.append_assistant(reply.content)

        self._submit_memory_extraction(
            self._get_memory(), user_text, reply.content
        )

        return reply

    def process_stream(
        self,
        user_text: str,
        on_emotion: Callable[[str], None] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AIReply:
        """Stream one reply, then atomically commit history and memory work."""
        started_at = time.monotonic()
        logger.info("voice_latency asr_text_entered")
        city = AIClient.extract_weather_city(user_text, self.default_city)
        memory = self._get_memory()
        history_snapshot = self.history.to_list()

        reply = self.client.process_stream(
            user_text=user_text,
            weather_city=city,
            conversation_history=history_snapshot,
            system_prompt=self.stream_system_prompt,
            fallback_system_prompt=self.system_prompt,
            on_emotion=on_emotion,
            on_text_delta=on_text_delta,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            from .client import StreamCancelled
            raise StreamCancelled()

        self.history.append_user(user_text)
        self.history.append_assistant(reply.content)
        self._submit_memory_extraction(memory, user_text, reply.content)
        logger.info(
            "voice_latency llm_complete=%.3fs", time.monotonic() - started_at
        )
        return reply

    def _submit_memory_extraction(
        self,
        memory: LongTermMemory,
        user_text: str,
        reply_content: str,
    ) -> Future | None:
        if self._closed:
            return None
        try:
            submitted_at = time.monotonic()
            future = self._memory_executor.submit(
                memory.extract_from_conversation,
                user_text,
                reply_content,
                self.client,
            )
        except RuntimeError:
            logger.info("Memory executor already shut down; skipping extraction")
            return None
        future.add_done_callback(
            lambda completed: self._log_memory_completion(
                completed, submitted_at
            )
        )
        return future

    @staticmethod
    def _log_memory_completion(future: Future, submitted_at: float) -> None:
        if future.cancelled():
            return
        try:
            future.result()
        except Exception:
            logger.exception("Unhandled background memory extraction error")
            return
        logger.info(
            "voice_latency memory_extraction_complete=%.3fs",
            time.monotonic() - submitted_at,
        )

    def close(self, wait: bool = False) -> None:
        """Stop accepting memory work without blocking normal UI shutdown."""
        if self._closed:
            return
        self._closed = True
        self._memory_executor.shutdown(wait=wait, cancel_futures=True)

    # ═══════════════════════════════════════════════════════════════════
    # 记忆管理
    # ═══════════════════════════════════════════════════════════════════

    def clear_memory(self) -> None:
        """清空对话记忆（开始新话题）。"""
        self.history.clear()

    def clear_long_term_memory(self, restore_starter: bool = False) -> None:
        """Clear the active character's persistent memory."""
        self._get_memory().clear(restore_starter=restore_starter)

    def get_memory_size(self) -> int:
        """返回当前记忆中的消息条数。"""
        return self.history.count()

    # ═══════════════════════════════════════════════════════════════════
    # 未来扩展接口（语音对话）
    # ═══════════════════════════════════════════════════════════════════

    def process_voice(self, audio_text: str) -> AIReply:
        """处理语音输入（未来接口）。

        当前实现：直接将语音识别结果当作文本输入处理。
        未来可以扩展：
        - 识别用户的语气（开心/生气）并影响情绪选择
        - 识别语速判断用户心情

        Args:
            audio_text: 语音识别（STT）后的文本

        Returns:
            AIReply 结构化回复
        """
        return self.process(audio_text)

    def get_reply_audio(self, text: str) -> bytes | None:
        """将回复文本转为音频（未来接口）。

        当前返回 None，未来接入 TTS 后返回音频数据。

        Args:
            text: 要转语音的文本

        Returns:
            音频 bytes（WAV/MP3），或 None（TTS 未实现）
        """
        # TODO: 接入 TTS 引擎
        # from audio import TTSEngine
        # return TTSEngine().synthesize(text)
        return None

    def can_chat(self) -> bool:
        """检查是否可以进行 AI 对话。

        如果没有配置 API Key，只能进行天气/时间查询。
        """
        return self.client._client is not None
