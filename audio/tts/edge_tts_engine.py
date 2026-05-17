# -*- coding: utf-8 -*-
"""
EdgeTTS 引擎 —— 基于微软 Edge 浏览器的免费 TTS

使用 edge-tts 库调用微软在线语音合成服务。

优点：
    - 完全免费，无需 API Key
    - 中文语音自然度高（晓晓、云希等角色）
    - 支持多种声音和语速调节

缺点：
    - 需要联网
    - 每次合成有网络延迟（约 0.5-2 秒）

声音角色说明：
    zh-CN-XiaoxiaoNeural  晓晓（女声，温柔）
    zh-CN-YunxiNeural      云希（男声，青年）    ← 当前默认
    zh-CN-YunjianNeural    云健（男声，成熟）
    zh-CN-XiaoyiNeural     晓伊（女声，活泼）
    zh-CN-YunyangNeural    云扬（男声，新闻播报）
    zh-CN-XiaochenNeural   晓辰（女声，自然）

后期替换为 SoVITS / CosyVoice 时，只需新建对应的 Engine 类即可，
UI 层完全不需要修改。
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import os

from .base_tts import TTSEngine


# =============================================================================
# EdgeTTS 引擎
# =============================================================================

class EdgeTTSEngine(TTSEngine):
    """基于 edge-tts 的中文语音合成引擎。

    用法：
        engine = EdgeTTSEngine(voice="zh-CN-YunxiNeural")
        audio_bytes = engine.synthesize("呆呆宝宝，找我啥事？")
        # audio_bytes 是 MP3 格式的二进制数据
    """

    def __init__(
        self,
        voice: str = "zh-CN-YunxiNeural",
        rate: str = "+0%",
        pitch: str = "+0Hz",
    ):
        """初始化 EdgeTTS 引擎。

        Args:
            voice: 声音角色 ID（默认云希男声）
            rate:  语速调整（"-50%" ~ "+100%"，默认不变速）
            pitch: 音调调整（"-20Hz" ~ "+20Hz"，默认不变调）
        """
        self._voice = voice
        self._rate = rate
        self._pitch = pitch

    @property
    def engine_name(self) -> str:
        return f"EdgeTTS ({self._voice})"

    @property
    def voice_id(self) -> str:
        return self._voice

    def synthesize(self, text: str) -> bytes:
        """将文字合成为 MP3 音频。

        使用 asyncio 在同步上下文中运行 edge-tts 的异步接口。
        如果当前已有事件循环则在其中调度，否则新建。

        Args:
            text: 要合成的文字

        Returns:
            MP3 音频的二进制数据
        """
        if not text or not text.strip():
            return b""

        try:
            # 获取或创建事件循环
            try:
                loop = asyncio.get_running_loop()
                # 已有事件循环（如在异步上下文中），用 nest_asyncio 或直接 await
                # 这里用 run_coroutine_threadsafe 或简单新建
                return asyncio.run(self._async_synthesize(text))
            except RuntimeError:
                # 无事件循环，直接 run
                return asyncio.run(self._async_synthesize(text))
        except Exception as exc:
            raise RuntimeError(f"EdgeTTS 合成失败: {exc}") from exc

    async def _async_synthesize(self, text: str) -> bytes:
        """异步调用 edge-tts 合成语音。

        由于 edge-tts 的 communicate() 方法会将 MP3 写入临时文件，
        我们通过临时文件收集后再读回 bytes。
        """
        import edge_tts

        communicate = edge_tts.Communicate(
            text=text,
            voice=self._voice,
            rate=self._rate,
            pitch=self._pitch,
        )

        # 使用临时文件收集音频数据
        temp_fd, temp_path = tempfile.mkstemp(suffix=".mp3")
        try:
            os.close(temp_fd)  # 关闭文件描述符，Communicate 会自己打开
            await communicate.save(temp_path)

            # 读回二进制数据
            with open(temp_path, "rb") as f:
                return f.read()
        finally:
            # 清理临时文件
            try:
                os.unlink(temp_path)
            except OSError:
                pass


# =============================================================================
# 预定义的便捷引擎实例
# =============================================================================

# 常用中文声音角色
EDGE_VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",   # 晓晓 - 温柔女声
    "yunxi":    "zh-CN-YunxiNeural",       # 云希 - 青年男声
    "yunjian":  "zh-CN-YunjianNeural",     # 云健 - 成熟男声
    "xiaoyi":   "zh-CN-XiaoyiNeural",      # 晓伊 - 活泼女声
    "yunyang":  "zh-CN-YunyangNeural",     # 云扬 - 新闻男声
    "xiaochen": "zh-CN-XiaochenNeural",    # 晓辰 - 自然女声
}


def create_edge_tts(voice: str = "xiaoxiao") -> EdgeTTSEngine:
    """快速创建 EdgeTTS 引擎的工厂函数。

    Args:
        voice: 声音名称（"xiaoxiao", "yunxi" 等）或完整 ID

    Returns:
        配置好的 EdgeTTSEngine 实例
    """
    voice_id = EDGE_VOICES.get(voice, voice)
    return EdgeTTSEngine(voice=voice_id)
