# -*- coding: utf-8 -*-
"""情绪数据定义模块 - 所有情绪、表情、别名的纯数据定义。

此模块仅包含数据常量和简单的 dataclass，不包含任何调度逻辑。
UI 层、AI 层、调度层均依赖此模块获取情绪元数据。

未来扩展语音模块时，可在此新增"说话中"等动画状态。
"""

from dataclasses import dataclass


# =============================================================================
# 情绪资源映射 (emotion_key → 中文描述)
# =============================================================================

EMOTION_ASSETS = {
    "NEUTRAL":     "待机、普通、日常回应",
    "HAPPY":       "开心、轻松、夸好",
    "SHY":         "害羞、不好意思、被夸到",
    "SLEEPY":      "困了、想睡、疲惫但还能聊",
    "THINKING":    "思考、疑惑、分析问题",
    "ANGRY":       "生气、不满、护短、吐槽",
    "SURPRISED":   "惊讶、震惊、突然发现",
    "SAD":         "难过、委屈、需要安慰",
    "LOVE":        "喜欢、心动、撒娇",
    "COOL":        "超酷、自信、装酷",
    "GREETING":    "打招呼、欢迎、召唤用户",
    "WORKING":     "工作中、认真处理任务",
    "READING":     "学习、看书、查资料",
    "EATING":      "吃东西、美食、热量话题",
    "DRINKING":    "喝水、提醒补水",
    "GOOD_NIGHT":  "晚安、睡前陪聊",
    "LISTENING":   "听你说、认真倾听",
    "HEART":       "比心、鼓励、感谢",
    "CELEBRATE":   "庆祝、完成任务、好消息",
    "TIRED":       "累趴了、压力大、崩溃但可爱",
}


# =============================================================================
# 情绪组别 (用于分析/统计，不影响调度)
# =============================================================================

EMOTION_GROUPS = {
    "HIGH":   ("NEUTRAL", "THINKING", "LISTENING", "WORKING", "GOOD_NIGHT", "TIRED"),
    "MEDIUM": ("HAPPY", "SHY", "SURPRISED", "SAD", "LOVE", "HEART"),
    "LOW":    ("ANGRY", "COOL", "EATING", "DRINKING", "CELEBRATE"),
}

# 待机环境动作（看书、工作、吃东西等）
AMBIENT_IDLE_EMOTIONS = ("READING", "WORKING", "EATING", "DRINKING")

# 夜间专属情绪
NIGHT_EMOTIONS = ("SLEEPY", "GOOD_NIGHT")

# 所有有效情绪 key 的集合
VALID_EMOTIONS = set(EMOTION_ASSETS)


# =============================================================================
# 情绪别名映射 (中文 → 英文 key)
# =============================================================================

EMOTION_ALIASES = {
    "待机":     "NEUTRAL",
    "开心":     "HAPPY",
    "害羞":     "SHY",
    "困了":     "SLEEPY",
    "思考":     "THINKING",
    "生气":     "ANGRY",
    "惊讶":     "SURPRISED",
    "难过":     "SAD",
    "喜欢":     "LOVE",
    "超酷":     "COOL",
    "打招呼":   "GREETING",
    "工作中":   "WORKING",
    "看书":     "READING",
    "吃东西":   "EATING",
    "喝水":     "DRINKING",
    "晚安":     "GOOD_NIGHT",
    "听你说":   "LISTENING",
    "给你比心": "HEART",
    "比心":     "HEART",
    "庆祝":     "CELEBRATE",
    "累趴了":   "TIRED",
    "超开心":   "HAPPY",
}

# 用于传入 System Prompt 的情绪列表
EMOTION_PROMPT = "\n".join(f"- {key}: {hint}" for key, hint in EMOTION_ASSETS.items())


# =============================================================================
# 情绪配置档案 (每条情绪的行为参数)
# =============================================================================

@dataclass(frozen=True)
class EmotionProfile:
    """单条情绪的配置档案。

    属性说明：
        key:         情绪标识（对应 assets 下的图片文件名）
        category:    分类（HIGH/MEDIUM/LOW）
        priority:    优先级（0-100，越高越优先抢占）
        duration_ms: 表情展示时长（毫秒，0=不自动恢复）
        cooldown_ms: 冷却时间（毫秒，避免同一表情连续出现）
        weight:      随机权重（越大越容易被选中）
    """
    key: str
    category: str
    priority: int
    duration_ms: int
    cooldown_ms: int
    weight: float = 1.0


EMOTION_PROFILES = {
    "NEUTRAL":     EmotionProfile("NEUTRAL",     "HIGH",   10, 0,    0,      8.0),
    "LISTENING":   EmotionProfile("LISTENING",   "HIGH",   95, 900,  1500,   6.0),
    "THINKING":    EmotionProfile("THINKING",    "HIGH",   100, 0,   1200,   6.0),
    "WORKING":     EmotionProfile("WORKING",     "HIGH",   45, 5200, 45000,  3.0),
    "GOOD_NIGHT":  EmotionProfile("GOOD_NIGHT",  "HIGH",   80, 6500, 240000, 2.0),
    "TIRED":       EmotionProfile("TIRED",       "HIGH",   78, 5200, 90000,  2.2),
    "HAPPY":       EmotionProfile("HAPPY",       "MEDIUM", 58, 3600, 25000,  3.2),
    "SHY":         EmotionProfile("SHY",         "MEDIUM", 68, 4300, 65000,  2.2),
    "SURPRISED":   EmotionProfile("SURPRISED",   "MEDIUM", 70, 3300, 60000,  2.0),
    "SAD":         EmotionProfile("SAD",         "MEDIUM", 74, 5200, 75000,  2.0),
    "LOVE":        EmotionProfile("LOVE",        "MEDIUM", 72, 4300, 90000,  1.8),
    "HEART":       EmotionProfile("HEART",       "MEDIUM", 82, 4800, 120000, 1.7),
    "ANGRY":       EmotionProfile("ANGRY",       "LOW",    55, 2800, 180000, 1.0),
    "COOL":        EmotionProfile("COOL",        "LOW",    48, 3800, 180000, 1.0),
    "EATING":      EmotionProfile("EATING",      "LOW",    42, 4500, 240000, 1.1),
    "DRINKING":    EmotionProfile("DRINKING",    "LOW",    42, 3800, 180000, 1.2),
    "SLEEPY":      EmotionProfile("SLEEPY",      "HIGH",   50, 4300, 60000,  2.5),
    "CELEBRATE":   EmotionProfile("CELEBRATE",   "MEDIUM", 48, 4800, 240000, 1.2),
    "READING":     EmotionProfile("READING",     "LOW",    30, 5200, 90000,  1.5),
    "GREETING":    EmotionProfile("GREETING",    "LOW",    44, 3000, 120000, 1.3),
}


# =============================================================================
# 情绪决策数据结构
# =============================================================================

@dataclass(frozen=True)
class EmotionDecision:
    """一次情绪决策的结果。

    属性说明：
        emotion:    最终选定的情绪 key
        duration_ms: 展示时长（毫秒）
        reason:     选用原因（调试用）
        category:   情绪类别
    """
    emotion: str
    duration_ms: int = 0
    reason: str = ""
    category: str = "NEUTRAL"


@dataclass(frozen=True)
class EmotionCandidate:
    """情绪候选（用于匹配和排序）。"""
    emotion: str
    reason: str
    priority_bonus: int = 0
    weight_multiplier: float = 1.0