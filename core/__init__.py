# -*- coding: utf-8 -*-
"""
Core 模块 - 宠物核心逻辑（与 UI 框架无关）

本模块包含老勺子的所有业务逻辑，不依赖任何 GUI 框架。
无论是桌面端(PyQt6)、命令行、还是嵌入式设备，
都可以直接复用 core 层的代码。

子模块说明：
    config.py       - 配置常量、API Key 加载
    conversation.py - 对话记忆管理
    client.py       - AI API 调用、天气预报、时间查询
    pet_brain.py    - 交互中枢，协调输入→处理→回复的完整链路
"""

from .config import PetConfig, load_api_key, APP_DIR, API_KEY_FILE
from .conversation import ConversationHistory
from .client import AIClient
from .pet_brain import PetBrain
from .long_term_memory import LongTermMemory

__all__ = [
    "PetConfig",
    "load_api_key",
    "APP_DIR",
    "API_KEY_FILE",
    "ConversationHistory",
    "AIClient",
    "PetBrain",
    "LongTermMemory",
]
