# -*- coding: utf-8 -*-
"""API Key 加载模块。

支持两种来源：
1. 环境变量 DEEPSEEK_API_KEY
2. api_key.txt 文件（位于项目根目录）

与 UI 框架无关，任何模块均可导入使用。
"""

import os


# ----------------------------------------------------------------
# 项目根目录中 api_key.txt 的绝对路径
# ----------------------------------------------------------------
API_KEY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api_key.txt")


def load_api_key() -> str:
    """加载 DeepSeek API Key。

    优先级：环境变量 DEEPSEEK_API_KEY > 同目录下的 api_key.txt 文件。
    """
    # 优先读取环境变量
    env_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return env_key

    # 回退到文件
    if not os.path.exists(API_KEY_FILE):
        return ""

    try:
        with open(API_KEY_FILE, "r", encoding="utf-8") as file:
            return file.read().strip()
    except OSError:
        return ""


def api_key_help_text() -> str:
    """生成缺少 API Key 时的提示文本。"""
    if os.path.exists(API_KEY_FILE) and os.path.getsize(API_KEY_FILE) == 0:
        return f"{API_KEY_FILE} 存在，但文件是空的。请把 DeepSeek API Key 粘贴进去并保存。"
    return f"普通聊天需要配置 DEEPSEEK_API_KEY，或在 {API_KEY_FILE} 中粘贴 DeepSeek API Key。"