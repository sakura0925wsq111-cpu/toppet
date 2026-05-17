# -*- coding: utf-8 -*-
"""天气查询模块 - 通过 wttr.in 免费 API 获取实时天气和预报。

所有天气查询在本机处理，不走大模型 API。
"""

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests

from .time_utils import WEEKDAY_NAMES


# ----------------------------------------------------------------
# 常量
# ----------------------------------------------------------------

# 触发天气查询的关键词
WEATHER_KEYWORDS = ("天气", "气温", "温度", "下雨", "下雪", "刮风", "冷不冷", "热不热", "weather", "temperature", "预报")


# ----------------------------------------------------------------
# 公共函数
# ----------------------------------------------------------------

def is_weather_question(text: str) -> bool:
    """判断用户是否在问天气，命中后直接走实时天气接口。"""
    lowered = text.lower()
    return any(keyword in lowered for keyword in WEATHER_KEYWORDS)


def extract_weather_day_offset(text: str) -> int:
    """从用户输入中提取天气日期偏移：0=今天/现在, 1=明天, 2=后天。"""
    lowered = text.lower()
    if any(w in lowered for w in ("后天", "day after tomorrow")):
        return 2
    if any(w in lowered for w in ("明天", "明日", "tomorrow")):
        return 1
    return 0


def get_weather(city: str) -> str:
    """查询指定城市的实时天气；失败时返回说明原因。"""
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

        desc = _weather_description(current)
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


def get_weather_forecast(city: str, day_offset: int) -> str:
    """查询天气预报（明天/后天）；使用 wttr.in 的 weather 数组。"""
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
        desc = _weather_description(hourly[mid_index]) if hourly else "天气情况未知"

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


def extract_weather_city(text: str) -> str | None:
    """从用户输入中尝试提取城市名称；提取不到时返回 None。"""
    # 中文城市名匹配
    cn_match = re.search(r"([一-龥]{2,12})(?:的)?(?:天气|气温|温度)", text)
    if cn_match:
        return clean_city_name(cn_match.group(1))

    # 英文城市名匹配
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
    return None


def clean_city_name(city: str) -> str | None:
    """去掉城市名中的口语前后缀，返回纯净的城市名。"""
    city = city.strip(" ，。！？?的")
    prefixes = ("帮我查一下", "帮我查查", "帮我查", "查一下", "查询", "查查", "看看", "看下", "告诉我", "请问", "今天", "明天", "后天", "现在", "当前")
    suffixes = ("今天", "明天", "后天", "现在", "当前", "这边", "那里", "哪里", "怎么样")

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


# ----------------------------------------------------------------
# 内部函数
# ----------------------------------------------------------------

def _weather_description(current: dict[str, Any]) -> str:
    """从 wttr.in 的返回值中优先取中文天气描述。"""
    for key in ("lang_zh", "weatherDesc"):
        values = current.get(key) or []
        if values and values[0].get("value"):
            return str(values[0]["value"])
    return "天气情况未知"