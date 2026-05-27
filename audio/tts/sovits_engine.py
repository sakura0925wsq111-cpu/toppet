# -*- coding: utf-8 -*-
"""
SoVITS TTS 引擎 —— 基于本地 GPT-SoVITS API

调用本地运行的 GPT-SoVITS API（默认 http://127.0.0.1:9880）进行语音合成。
启动时自动通过 /set_model 统一接口切换到用户专属模型。
"""

from __future__ import annotations

import requests

from .base_tts import TTSEngine


class SoVITSTTSEngine(TTSEngine):
    """基于 GPT-SoVITS 本地 API 的中文语音合成引擎。"""

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:9880",
        gpt_model_path: str = "GPT_weights_v2Pro/吴绍强-e15.ckpt",
        sovits_model_path: str = "SoVITS_weights_v2pro/吴绍强_e8_s160.pth",
        ref_audio_path: str = "",
        prompt_text: str = "",
        prompt_language: str = "zh",
        text_language: str = "zh",
        timeout: int = 30,
    ):
        self._api_url = api_url.rstrip("/")
        self._gpt_model_path = gpt_model_path
        self._sovits_model_path = sovits_model_path
        self._ref_audio_path = ref_audio_path
        self._prompt_text = prompt_text
        self._prompt_language = prompt_language
        self._text_language = text_language
        self._timeout = timeout
        self._model_set = False

    @property
    def engine_name(self) -> str:
        return f"GPT-SoVITS (localhost:9880)"

    @property
    def voice_id(self) -> str:
        return self._sovits_model_path

    # ── 模型切换 ──

    def set_model(self) -> bool:
        """调用 /set_model 统一接口切换模型；失败时自动回退到旧接口。"""
        try:
            # ── 方案A：新统一接口 POST /set_model ──
            resp = requests.post(
                f"{self._api_url}/set_model",
                json={
                    "vits_route": self._gpt_model_path,
                    "t2s_route": self._sovits_model_path,
                },
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                print(f"[SoVITS] /set_model OK  GPT={self._gpt_model_path}  SoVITS={self._sovits_model_path}")
                self._model_set = True
                return True

            # ── 方案B：交换 vits_route / t2s_route 重试 ──
            print(f"[SoVITS] /set_model HTTP {resp.status_code}: {resp.text[:200]}")
            print("[SoVITS] Retrying with swapped vits_route/t2s_route...")
            resp2 = requests.post(
                f"{self._api_url}/set_model",
                json={
                    "vits_route": self._sovits_model_path,
                    "t2s_route": self._gpt_model_path,
                },
                timeout=self._timeout,
            )
            if resp2.status_code == 200:
                print(f"[SoVITS] /set_model OK (swapped)  vits={self._sovits_model_path}  t2s={self._gpt_model_path}")
                self._model_set = True
                return True

            # ── 方案C：回退到旧版独立接口 ──
            print(f"[SoVITS] Swapped also failed ({resp2.status_code}), falling back to legacy endpoints...")
            return self._set_model_legacy()

        except requests.ConnectionError:
            print(f"[SoVITS] Cannot connect to {self._api_url}")
            return False
        except Exception as exc:
            print(f"[SoVITS] set_model error: {exc}")
            return False

    def _set_model_legacy(self) -> bool:
        """旧版回退：分别调用 /set_gpt_weights 和 /set_sovits_weights。"""
        try:
            resp = requests.get(
                f"{self._api_url}/set_gpt_weights",
                params={"weights_path": self._gpt_model_path},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                print(f"[SoVITS] set_gpt_weights HTTP {resp.status_code}: {resp.text[:200]}")
                return False
            print(f"[SoVITS] GPT weights set: {self._gpt_model_path}")

            resp = requests.get(
                f"{self._api_url}/set_sovits_weights",
                params={"weights_path": self._sovits_model_path},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                print(f"[SoVITS] set_sovits_weights HTTP {resp.status_code}: {resp.text[:200]}")
                return False
            print(f"[SoVITS] SoVITS weights set: {self._sovits_model_path}")
            print("[SoVITS] Model switch OK (legacy)!")
            return True
        except Exception as exc:
            print(f"[SoVITS] set_model_legacy error: {exc}")
            return False

    # ── 合成 ──

    def synthesize(self, text: str) -> bytes:
        """将文字合成为 WAV 音频。"""
        if not text or not text.strip():
            return b""

        params = {
            "text": text,
            "text_lang": self._text_language,
        }
        if self._ref_audio_path:
            params["ref_audio_path"] = self._ref_audio_path
        if self._prompt_text:
            params["prompt_text"] = self._prompt_text
        if self._prompt_language:
            params["prompt_lang"] = self._prompt_language

        try:
            resp = requests.get(
                f"{self._api_url}/tts",
                params=params,
                timeout=self._timeout,
            )
        except requests.Timeout:
            raise RuntimeError(f"SoVITS TTS timeout ({self._timeout}s)")
        except requests.ConnectionError:
            raise RuntimeError(f"Cannot connect to {self._api_url}")

        if resp.status_code != 200 or not resp.content:
            try:
                resp = requests.post(
                    f"{self._api_url}/tts",
                    json=params,
                    timeout=self._timeout,
                )
            except Exception as exc:
                raise RuntimeError(f"SoVITS TTS POST fallback failed: {exc}")

        if resp.status_code != 200:
            raise RuntimeError(
                f"SoVITS TTS HTTP {resp.status_code}: {resp.text[:200]}"
            )

        if not resp.content:
            raise RuntimeError("SoVITS TTS returned empty audio")

        return resp.content


def create_sovits_tts(
    api_url: str = "http://127.0.0.1:9880",
    gpt_model: str = "GPT_weights_v2Pro/吴绍强-e15.ckpt",
    sovits_model: str = "SoVITS_weights_v2pro/吴绍强_e8_s160.pth",
) -> SoVITSTTSEngine:
    """快速创建 SoVITS TTS 引擎。"""
    return SoVITSTTSEngine(
        api_url=api_url,
        gpt_model_path=gpt_model,
        sovits_model_path=sovits_model,
    )
