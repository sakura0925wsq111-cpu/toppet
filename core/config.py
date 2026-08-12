# -*- coding: utf-8 -*-
"""
宠物核心配置 —— 常量、API Key 加载、运行时参数

所有配置数据和工具函数集中在此，不依赖任何 GUI 框架。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from common.paths import (
    API_KEY_FILE as API_KEY_PATH,
    ASSETS_DIR as ASSETS_PATH,
    PROJECT_ROOT,
)

# =============================================================================
# 路径常量
# =============================================================================

# Keep these public constants as strings for backward compatibility. New code
# should import pathlib-based locations from ``common.paths`` directly.
APP_DIR: str = str(PROJECT_ROOT)
API_KEY_FILE: str = str(API_KEY_PATH)
ASSETS_DIR: str = str(ASSETS_PATH)


# =============================================================================
# 字符串常量
# =============================================================================

# 星期名称（中文）
WEEKDAY_NAMES: tuple[str, ...] = (
    "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"
)

# 天气相关关键词（用于判断用户是否在问天气）
WEATHER_KEYWORDS: tuple[str, ...] = (
    "天气", "气温", "温度", "下雨", "下雪", "刮风",
    "冷不冷", "热不热", "weather", "temperature", "预报",
)

# 交互状态文字（显示在状态栏）
INTERACTION_STATUS_TEXT: dict[str, str] = {
    "idle": "待机中",
    "listening": "听你说",
    "thinking": "思考中",
}


# =============================================================================
# API Key 加载
# =============================================================================

def load_api_key() -> str:
    """加载 DeepSeek API Key。

    优先级：
    1. 环境变量 DEEPSEEK_API_KEY
    2. 同目录下的 api_key.txt 文件

    Returns:
        API Key 字符串；未配置时返回空字符串。
    """
    # 优先读环境变量
    env_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return env_key

    # 读文件
    if not os.path.exists(API_KEY_FILE):
        return ""

    try:
        with open(API_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def api_key_help_text() -> str:
    """生成缺少 API Key 时的提示信息。

    区分两种情况：文件不存在 vs 文件为空。
    """
    if os.path.exists(API_KEY_FILE) and os.path.getsize(API_KEY_FILE) == 0:
        return (
            f"{API_KEY_FILE} 存在，但文件是空的。"
            f"请把 DeepSeek API Key 粘贴进去并保存。"
        )
    return (
        f"普通聊天需要配置 DEEPSEEK_API_KEY，"
        f"或在 {API_KEY_FILE} 中粘贴 DeepSeek API Key。"
    )


# =============================================================================
# PetConfig —— 运行时配置
# =============================================================================

@dataclass
class PetConfig:
    """宠物运行时配置。

    所有可变参数集中在这里，方便在不同环境中切换。
    如果未来移植到嵌入式设备，只需修改这里的默认值即可。
    """

    # --- 基础标识 ---
    name: str = "老勺子"
    default_emotion: str = "NEUTRAL"
    default_city: str = "上海"

    # --- API 配置 ---
    api_base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    # 不建议把 API Key 直接写进代码，运行时动态加载
    api_key: str = ""  # 由 __post_init__ 自动填充

    # --- 对话配置 ---
    max_conversation_rounds: int = 15  # 最多保留 N 轮对话记忆
    ai_temperature: float = 0.9
    ai_max_tokens: int = 260

    def __post_init__(self) -> None:
        """自动从环境 / 文件加载 API Key。"""
        if not self.api_key:
            self.api_key = load_api_key()
