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

from .config import PetConfig
from .conversation import ConversationHistory
from .client import AIClient, AIReply

# 延迟导入，避免循环依赖
from emotion_scheduler import EMOTION_PROMPT


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

    def __init__(self, config: PetConfig | None = None):
        """初始化大脑。

        Args:
            config: 宠物配置。如果为 None，使用默认配置。
        """
        if config is None:
            config = PetConfig()
        self.config = config

        # ── 创建 AI 客户端 ──
        self.client = AIClient(
            api_key=config.api_key,
            base_url=config.api_base_url,
            model=config.model,
            temperature=config.ai_temperature,
            max_tokens=config.ai_max_tokens,
        )

        # ── 创建对话记忆 ──
        self.history = ConversationHistory(
            max_rounds=config.max_conversation_rounds,
        )

        # ── 默认城市 ──
        self.default_city = config.default_city

    # ═══════════════════════════════════════════════════════════════════
    # 性格设定（System Prompt）
    # ═══════════════════════════════════════════════════════════════════

    @property
    def system_prompt(self) -> str:
        """构建性格设定提示词。

        每次调用都会动态生成（包含当前时间），
        确保 AI 知道"现在几点"而不会编造。
        """
        now_str = AIClient.get_system_time()
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

    # ═══════════════════════════════════════════════════════════════════
    # 主接口：处理用户输入
    # ═══════════════════════════════════════════════════════════════════

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

        return reply

    # ═══════════════════════════════════════════════════════════════════
    # 记忆管理
    # ═══════════════════════════════════════════════════════════════════

    def clear_memory(self) -> None:
        """清空对话记忆（开始新话题）。"""
        self.history.clear()

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
