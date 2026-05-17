# -*- coding: utf-8 -*-
"""时间工具模块 - 提供系统时间获取和时间意图检测。

负责回答用户关于"几点"、"星期几"、"今天几号"等问题，
全部在本机处理，不依赖网络 API。
"""

import re
from datetime import datetime


# ----------------------------------------------------------------
# 常量
# ----------------------------------------------------------------

WEEKDAY_NAMES = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ----------------------------------------------------------------
# 公共函数
# ----------------------------------------------------------------

def get_system_time() -> str:
    """获取当前本机时间，返回格式化的中文字符串。"""
    now = datetime.now()
    weekday = WEEKDAY_NAMES[now.weekday()]
    return f"{now:%Y年%m月%d日} {weekday} {now:%H:%M:%S}"


def is_time_question(text: str) -> bool:
    """判断用户输入是否为时间/日期类问题。"""
    lowered = text.lower()
    patterns = [
        r"(几点|几号|星期几|礼拜几|周几)",
        r"(现在|当前).*(时间|几点|日期)",
        r"(今天|明天|后天).*(日期|几号|星期|周几|礼拜)",
        r"\b(time|date)\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)