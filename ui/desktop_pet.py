# -*- coding: utf-8 -*-
"""
DesktopPet —— 桌面宠物主窗口（纯 UI 层）

基于 PyQt6 的无边框置顶窗口，负责：
1. 宠物图片显示与情绪切换
2. 对话输入框与输出气泡
3. 语音输入按钮（按住说话）
4. 语音输出（AI 回复自动播放）
5. 鼠标拖拽移动窗口
6. 右键菜单退出

本模块是"薄"UI 层 —— 所有业务逻辑由 core.PetBrain 处理，
语音能力由 audio.TTSManager / audio.ASRManager 处理，
本层只负责渲染和用户交互。
"""

from __future__ import annotations

import os
import logging
import re
import time

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QMovie, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.config import PetConfig, api_key_help_text
from core.pet_brain import PetBrain

from systems.emotion_scheduler import (
    EMOTION_ALIASES,
    EMOTION_ASSETS,
    EmotionDecision,
    PetEmotionScheduler,
)

from ui.ai_worker import AIWorker

# ── 语音模块 ──
from audio import (
    ASRManager,
    EDGE_VOICES,
    EdgeTTSEngine,
    FallbackTTSEngine,
    TTSManager,
    VoxCPMTTSEngine,
)
from systems.voxcpm_service_manager import VoxCPMServiceManager
from audio.tts.qwen_engine import QwenTTSEngine, resolve_tts_config

logger = logging.getLogger(__name__)

# =============================================================================
# 交互状态文字
# =============================================================================

INTERACTION_STATUS_TEXT = {
    "idle": "待机中",
    "listening": "听你说",
    "thinking": "思考中",
    "talking": "说话中",    # ── 新增：TTS 播放时
    "recording": "录音中",  # ── 新增：语音输入时
}


# =============================================================================
# DesktopPet —— 桌宠主窗口
# =============================================================================

class DesktopPet(QWidget):
    """桌面宠物主窗口。

    无边框、置顶、透明背景。
    支持文字输入 + 语音输入（按住说话）。
    AI 回复自动通过 TTS 朗读。
    """

    def __init__(
        self,
        config: PetConfig,
        brain: PetBrain,
        mode_id: str = "personal",
        start_background_services: bool = True,
    ):
        super().__init__()
        self.config = config
        self.brain = brain
        self.mode_id = mode_id
        self._start_background_services = start_background_services
        self.drag_offset = None

        # A DesktopPet instance represents exactly one mode. The UI shares the
        # brain's manager so persona, memory, assets and voice cannot drift.
        self.brain.current_character = mode_id
        self.character_manager = self.brain.character_manager
        self.mode_profile = self.character_manager.get_profile(mode_id)
        self.user_address = str(
            self.mode_profile.get("user_address", self.brain.user_address)
        ).strip() or "用户"

        # ── 检查 API Key ──
        if not self.config.api_key:
            QMessageBox.warning(
                self,
                "缺少 API Key",
                api_key_help_text() + "\n\n时间和天气查询仍可使用。",
            )

        # ── 图片缓存 ──
        self.movies: dict[str, QMovie] = {}
        self.pixmaps: dict[str, QPixmap] = {}
        self.current_movie: QMovie | None = None
        self._character_expressions: dict[str, set[str]] = {}

        # ── 情绪调度器 ──
        self.emotion_scheduler = PetEmotionScheduler()

        self.emotion_return_token = 0
        self.is_ai_busy = False
        self.is_tts_playing = False   # TTS
        self._tts_pending = False     # 已提交合成、等待开始播放
        self._tts_emotion = "NEUTRAL"    # 缓存 AI 情感，TTS 播放时切表情
        self._tts_text = ""              # 缓存 AI 回复文字，与语音同步显示
        self._voxcpm_ready = False
        self.interaction_state = "idle"
        self.worker: AIWorker | None = None
        self._stream_session_counter = 0
        self._active_stream_session: int | None = None
        self._stream_text = ""
        self._stream_display_dirty = False
        self._stream_tts_started = False
        self._queued_tts_text = ""
        self._request_started_at = 0.0

        # ── 语音管理器（TTS + ASR）──
        self.tts_manager = TTSManager()
        self._configure_tts_for_mode()

        # The cloned voice loads asynchronously; EdgeTTS is usable immediately.
        self.tts_service: VoxCPMServiceManager | None = None
        self.asr_manager = ASRManager()
        self._connect_voice_signals()

        if self._start_background_services:
            self.asr_manager.preload()

        # ── 初始化 UI ──
        self._setup_ui()
        self._load_images()

        self._stream_display_timer = QTimer(self)
        self._stream_display_timer.setInterval(40)
        self._stream_display_timer.timeout.connect(self._flush_stream_display)

        if self._start_background_services:
            self._start_optional_tts_service()

        # ── 启动默认表情 ──
        self.apply_emotion_decision(
            self.emotion_scheduler.default_decision(reason="启动后进入待机"),
            schedule_return=False,
        )
        self.emotion_scheduler.start_idle_clock()

        # ── 空闲定时器 ──
        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(1_000)
        self.idle_timer.timeout.connect(self._on_idle_tick)
        self.idle_timer.start()

    # TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTT
    # TTS Service lifecycle callbacks
    # TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTT

    def _on_tts_service_ready(self):
        """Mark the configured cloned-voice service ready."""
        self._voxcpm_ready = True
        print("[DesktopPet] Configured cloned voice ready")

    def _on_tts_service_failed(self, msg):
        """Keep the personal voice silent rather than substituting another voice."""
        print(f"[DesktopPet] Cloned speech unavailable: {msg}")

    def _on_tts_service_status(self, msg):
        """Report background model loading without blocking the UI."""
        print(f"[DesktopPet] Voice service: {msg}")

    def _start_optional_tts_service(self) -> None:
        """Start VoxCPM2 only for the personal/Laoshaozi profile."""
        tts_config = resolve_tts_config(self.mode_profile)
        if tts_config.get("engine") == "qwen":
            from systems.qwen_service_manager import QwenServiceManager
            service = QwenServiceManager(tts_config)
            service.service_ready.connect(self._on_tts_service_ready)
            service.service_failed.connect(self._on_tts_service_failed)
            service.status_update.connect(self._on_tts_service_status)
            self.tts_service = service
            service.start_service()
            return
        if tts_config.get("engine") != "voxcpm" or not tts_config.get(
            "optional_voxcpm", False
        ):
            return

        service_kwargs = {
            "api_url": tts_config.get("api_url", "http://127.0.0.1:9890"),
            "inference_steps": int(tts_config.get("inference_steps", 4)),
        }
        reference_path = self.character_manager.get_pack_file(
            tts_config.get("reference_audio"), self.mode_id
        )
        if reference_path:
            service_kwargs["reference_path"] = reference_path
        service = VoxCPMServiceManager(**service_kwargs)
        service.service_ready.connect(self._on_tts_service_ready)
        service.service_failed.connect(self._on_tts_service_failed)
        service.status_update.connect(self._on_tts_service_status)
        self.tts_service = service

        service.start_service()

    def shutdown_services(self) -> None:
        """Stop background audio work. Safe to call more than once."""
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel_request()
            self.worker.wait(500)
        self._stream_display_timer.stop()
        self.tts_manager.shutdown()
        self.asr_manager.cancel()
        self.brain.close(wait=False)
        if self.tts_service is not None:
            self.tts_service.stop_service()

    def closeEvent(self, event):
        """Shutdown background services before closing the window."""
        self.shutdown_services()
        super().closeEvent(event)

    # ═══════════════════════════════════════════════════════════════════
    # 语音信号连接
    # ═══════════════════════════════════════════════════════════════════

    def _connect_voice_signals(self) -> None:
        """连接 TTS 和 ASR 的信号到 UI 更新方法。

        这些连接让语音模块可以驱动表情切换和状态变化，
        而语音模块本身不需要知道 UI 的存在。
        """
        # ── TTS 信号 ──
        # 开始播放语音 → 切 TALKING 表情
        self.tts_manager.playback_started.connect(self._on_tts_started)
        # 播放完成 → 恢复 idle
        self.tts_manager.playback_finished.connect(self._on_tts_finished)
        # 播放出错 → 恢复 idle（静默处理，不影响体验）
        self.tts_manager.playback_error.connect(self._on_tts_error)

        # ── ASR 信号 ──
        # 开始录音 → 切 LISTENING 表情
        self.asr_manager.recording_started.connect(self._on_recording_started)
        # 开始识别 → 切 THINKING 表情
        self.asr_manager.recognition_started.connect(self._on_recognition_started)
        # 识别完成 → 把文字喂给 _on_send()
        self.asr_manager.recognition_done.connect(self._on_voice_text_ready)
        # 识别出错 → 恢复 idle
        self.asr_manager.error_occurred.connect(self._on_voice_error)

    # ═══════════════════════════════════════════════════════════════════
    # UI 搭建
    # ═══════════════════════════════════════════════════════════════════

    def _setup_ui(self) -> None:
        """搭建无边框窗口和所有子控件。"""

        # ── 窗口属性：无边框、置顶、透明背景 ──
        self.setWindowTitle(
            self.mode_profile.get(
                "display_name", self.config.name
            )
        )
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # ── 主布局 ──
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        # ── 宠物图片展示区 ──
        self.pet_label = QLabel()
        self.pet_label.setFixedSize(220, 220)
        self.pet_label.setStyleSheet("background: transparent;")
        self.pet_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ── 对话输出气泡 ──
        ui_config = self.mode_profile.get("ui", {})
        display_name = self.character_manager.get_display_name()
        greeting = ui_config.get("greeting", f"你好，我是{display_name}。")
        self.chat_output = QLabel(greeting)
        self.chat_output.setWordWrap(True)
        self.chat_output.setStyleSheet(
            """
            QLabel {
                color: white;
                background: rgba(0, 0, 0, 150);
                border-radius: 10px;
                padding: 10px;
                font-size: 13px;
            }
            """
        )

        # ── 对话输入框 ──
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText(
            ui_config.get("input_placeholder", f"和{display_name}说点什么…")
        )
        self.chat_input.setStyleSheet(
            """
            QLineEdit {
                color: white;
                background: rgba(0, 0, 0, 120);
                border: 1px solid rgba(255, 255, 255, 80);
                border-radius: 10px;
                padding: 6px 10px;
            }
            """
        )
        self.chat_input.returnPressed.connect(self._on_send)
        self.chat_input.textEdited.connect(self._on_input_edited)

        # ── 🎤 语音按钮（按住说话，松开识别）──
        self.voice_button = QPushButton("🎤 按住说话")
        self.voice_button.setStyleSheet(
            """
            QPushButton {
                color: white;
                background: rgba(0, 200, 100, 140);
                border-radius: 10px;
                padding: 8px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:pressed {
                background: rgba(255, 60, 60, 180);
            }
            """
        )
        # 按住开始录音，松开停止录音
        self.voice_button.pressed.connect(self._on_voice_button_pressed)
        self.voice_button.released.connect(self._on_voice_button_released)

        # ── 依次添加到主布局 ──
        root_layout.addWidget(self.pet_label)
        root_layout.addWidget(self.chat_output)
        root_layout.addWidget(self.chat_input)
        root_layout.addWidget(self.voice_button)

        # ── 调整窗口尺寸（多加一个按钮高度）──
        self.resize(240, 395)

    # ═══════════════════════════════════════════════════════════════════
    # 图片加载与表情切换（保持不变）
    # ═══════════════════════════════════════════════════════════════════

    def _load_images(self) -> None:
        """Load expression images for this instance's fixed mode only.

        Asset structure:
            assets/
            +-- personal/
            |   +-- NEUTRAL.gif / NEUTRAL.png
            |   +-- HAPPY.gif   / HAPPY.png
            |   +-- ...
        Cache key format: "character/EXPRESSION" (e.g. "personal/HAPPY").
        """
        self.movies.clear()
        self.pixmaps.clear()
        self._character_expressions.clear()
        missing: list[str] = []

        active_mode = self.character_manager.current_character
        for char_id in (active_mode,):
            char_dir = self.character_manager.get_character_asset_dir(char_id)
            if not os.path.isdir(char_dir):
                continue

            self._character_expressions.setdefault(char_id, set())
            seen: set[str] = set()

            for fname in os.listdir(char_dir):
                name, ext = os.path.splitext(fname)
                ext_lower = ext.lower()
                if ext_lower not in (".gif", ".png", ".jpg", ".jpeg"):
                    continue

                expr_key = name.upper()
                cache_key = f"{char_id}/{expr_key}"

                if cache_key in seen:
                    continue  # prefer first found (gif over png)
                seen.add(cache_key)

                filepath = os.path.join(char_dir, fname)
                if ext_lower == ".gif":
                    movie = QMovie(filepath)
                    if movie.isValid():
                        movie.setCacheMode(QMovie.CacheMode.CacheAll)
                        movie.setScaledSize(self.pet_label.size())
                        self.movies[cache_key] = movie
                        self._character_expressions[char_id].add(expr_key)
                else:
                    pixmap = QPixmap(filepath)
                    if not pixmap.isNull():
                        self.pixmaps[cache_key] = pixmap
                        self._character_expressions[char_id].add(expr_key)

            if "NEUTRAL" not in self._character_expressions.get(char_id, set()):
                missing.append(f"{char_id}/NEUTRAL")

        if missing:
            self.chat_output.setText("missing: " + ", ".join(missing))

    def set_emotion(self, emotion: str) -> None:
        """Switch pet image based on unified emotion key, resolved through character manager.

        Flow:
            unified emotion (e.g. "HAPPY")
              -> character_manager.resolve()
            character expression (e.g. shinchan "HAPPY")
            -> cache lookup "shinchan/HAPPY"
            -> display

        Missing asset fallback:
            character/expression -> character/NEUTRAL
        """
        raw_emotion = str(emotion).strip()
        emotion = EMOTION_ALIASES.get(
            raw_emotion,
            re.sub(r"[\s-]+", "_", raw_emotion.upper()),
        )

        # Resolve through character manager
        expression = self.character_manager.resolve(emotion)
        char = self.character_manager.current_character

        # Fallback-aware cache lookup
        cache_key = self._find_asset_key(char, expression)
        if cache_key is None:
            return

        movie = self.movies.get(cache_key)
        if movie and movie.isValid():
            if self.current_movie and self.current_movie is not movie:
                self.current_movie.stop()
            self.current_movie = movie
            movie.setScaledSize(self.pet_label.size())
            self.pet_label.setMovie(movie)
            movie.start()
            return

        if self.current_movie:
            self.current_movie.stop()
            self.current_movie = None

        pixmap = self.pixmaps.get(cache_key)
        if not pixmap or pixmap.isNull():
            return

        scaled = pixmap.scaled(
            self.pet_label.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.pet_label.setPixmap(scaled)

    def _find_asset_key(self, char: str, expression: str) -> str | None:
        """Find the best available asset key with fallback chain.

        Fallback order:
            1. {char}/{expression}
            2. {char}/NEUTRAL
        """
        candidates = [
            f"{char}/{expression}",
            f"{char}/NEUTRAL",
        ]
        for key in candidates:
            if key in self.movies or key in self.pixmaps:
                return key
        return None

    def apply_emotion_decision(
        self, decision: EmotionDecision, schedule_return: bool = True
    ) -> None:
        """应用调度器给出的表情。"""
        self.emotion_return_token += 1
        token = self.emotion_return_token
        self.set_emotion(decision.emotion)

        if schedule_return and decision.duration_ms > 0:
            QTimer.singleShot(
                decision.duration_ms,
                lambda: self._return_to_idle(token),
            )

    def _return_to_idle(self, token: int) -> None:
        """在短表情展示完毕后回到待机态。"""
        if token != self.emotion_return_token or self.is_ai_busy:
            return
        if self.is_tts_playing or self._tts_pending:
            return  # TTS 播放中不回 idle
        decision = self.emotion_scheduler.default_decision(
            reason="短表情展示完毕，回到待机", skip_night=True
        )
        self.apply_emotion_decision(decision, schedule_return=False)

    # ═══════════════════════════════════════════════════════════════════
    # 空闲行为
    # ═══════════════════════════════════════════════════════════════════

    def _on_idle_tick(self) -> None:
        """每秒定时检查是否需要进行空闲行为。"""
        if self.is_ai_busy or self.is_tts_playing:
            return
        if not self.chat_input.isEnabled():
            return
        if self.asr_manager.is_recording():
            return  # 录音中不触发空闲
        decision = self.emotion_scheduler.maybe_idle()
        if decision:
            self.set_emotion(decision.emotion)

    # ═══════════════════════════════════════════════════════════════════
    # 交互状态管理
    # ═══════════════════════════════════════════════════════════════════

    def _set_interaction_state(self, state: str) -> None:
        """更新桌宠交互状态。"""
        if state not in INTERACTION_STATUS_TEXT:
            state = "idle"
        self.interaction_state = state

    def _on_input_edited(self, text: str) -> None:
        """用户正在输入文字时进入"听你说"状态。"""
        if self.is_ai_busy or self.is_tts_playing:
            return

        if text.strip():
            if self.interaction_state != "listening":
                self._set_interaction_state("listening")
                self.apply_emotion_decision(
                    self.emotion_scheduler.on_user_input(text),
                    schedule_return=False,
                )
            return

        self._set_interaction_state("idle")
        self.apply_emotion_decision(
            self.emotion_scheduler.default_decision(reason="输入框清空，回到待机"),
            schedule_return=False,
        )

    # ═══════════════════════════════════════════════════════════════════
    # 发送消息与 AI 交互
    # ═══════════════════════════════════════════════════════════════════

    def _on_send(self, text: str | None = None) -> None:
        """发送消息给 AI（同时支持文字和语音输入）。

        Args:
            text: 消息文字。如果为 None，从输入框获取。
                  语音识别完成后会传入识别文本。
        """
        if text is None:
            text = self.chat_input.text().strip()
        if not text:
            return

        # 更新界面状态
        self.chat_input.clear()
        self.chat_input.setEnabled(False)
        self.voice_button.setEnabled(False)  # ── 思考期间禁用语音按钮
        self.is_ai_busy = True
        self._request_started_at = time.monotonic()
        self.chat_output.setText(f"你：{text}")
        self._start_ai_request(text)

    def _start_ai_request(self, text: str) -> None:
        """从"听你说"过渡到"思考中"，启动后台线程。"""
        if not self.is_ai_busy:
            return

        self._set_interaction_state("thinking")
        self.apply_emotion_decision(
            self.emotion_scheduler.on_ai_start(),
            schedule_return=False,
        )

        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel_request()
        self.tts_manager.cancel_stream()
        self._stream_session_counter += 1
        self._active_stream_session = self._stream_session_counter
        self._stream_text = ""
        self._stream_display_dirty = False
        self._stream_tts_started = False
        self._queued_tts_text = ""
        self._tts_text = ""
        self._tts_pending = False
        self.is_tts_playing = False

        self.worker = AIWorker(self.brain, text, self)
        self.worker.emotion_ready.connect(self._on_stream_emotion)
        self.worker.text_delta.connect(self._on_stream_text_delta)
        self.worker.sentence_ready.connect(self._on_stream_sentence)
        self.worker.response_ready.connect(self._on_ai_response)
        self.worker.error_occurred.connect(self._on_ai_error)
        self.worker.start()

    def _is_current_worker_signal(self) -> bool:
        sender = self.sender()
        return sender is None or sender is self.worker

    def _on_stream_emotion(self, emotion: str) -> None:
        if not self._is_current_worker_signal():
            return
        self._tts_emotion = emotion or "NEUTRAL"

    def _on_stream_text_delta(self, delta: str) -> None:
        if not self._is_current_worker_signal() or not delta:
            return
        self._stream_text += delta
        self._tts_text = self._stream_text.strip()
        self._stream_display_dirty = True
        if not self._stream_display_timer.isActive():
            self._stream_display_timer.start()

    def _flush_stream_display(self) -> None:
        if not self._stream_display_dirty:
            if not self.is_ai_busy:
                self._stream_display_timer.stop()
            return
        self._stream_display_dirty = False
        self.chat_output.setText(self._stream_text.strip())
        if not self.is_ai_busy:
            self._stream_display_timer.stop()

    def _on_stream_sentence(self, sentence: str) -> None:
        if not self._is_current_worker_signal() or not sentence.strip():
            return
        session_id = self._active_stream_session
        if session_id is None:
            return
        if not self._stream_tts_started:
            self.tts_manager.begin_stream(session_id)
            self._stream_tts_started = True
            self._tts_pending = True
        if self.tts_manager.enqueue_text(session_id, sentence):
            self._queued_tts_text += sentence

    def _on_ai_response(self, data: dict) -> None:
        """AI 成功返回回复时的处理。

        在显示文字后，自动调用 TTS 朗读回复。
        """
        if not self._is_current_worker_signal():
            return
        emotion = data.get("emotion", self._tts_emotion or "NEUTRAL")
        content = data.get(
            "content",
            f"{self.user_address}，我刚刚脑子短路了，再问我一次～",
        )
        user_text = getattr(self.worker, "user_text", "") if self.worker else ""

        # 恢复界面
        self.is_ai_busy = False
        self._stream_text = content
        self._tts_text = content
        self._stream_display_dirty = True
        self._flush_stream_display()

        # 更新表情
        decision = self.emotion_scheduler.on_reply(user_text, content, emotion)
        self.apply_emotion_decision(decision, schedule_return=False)

        self._tts_emotion = decision.emotion

        # 恢复输入（语音按钮在 TTS 播放结束后恢复）
        self.chat_input.setEnabled(True)
        self.chat_input.setFocus()
        if self._stream_tts_started and self._active_stream_session is not None:
            self.tts_manager.end_stream(self._active_stream_session)
            if not self.is_tts_playing:
                self._set_interaction_state("thinking")
        else:
            # Defensive fallback for an unusual empty/malformed stream.
            self._set_interaction_state("idle")
            self.voice_button.setEnabled(True)

    def _on_ai_error(self, err: str) -> None:
        """AI 处理失败时的兜底处理。"""
        if not self._is_current_worker_signal():
            return
        self.is_ai_busy = False
        if self._active_stream_session is not None:
            self.tts_manager.cancel_stream(self._active_stream_session)
        self._active_stream_session = None
        self._stream_tts_started = False
        self.apply_emotion_decision(self.emotion_scheduler.on_error())
        self.chat_output.setText(
            f"{self.user_address}，我这边处理失败了：" + err[:80]
        )
        self.chat_input.setEnabled(True)
        self.chat_input.setFocus()
        self.voice_button.setEnabled(True)
        self._set_interaction_state("idle")

    # ═══════════════════════════════════════════════════════════════════
    # 🎤 语音输入处理（新增）
    # ═══════════════════════════════════════════════════════════════════

    def _on_voice_button_pressed(self) -> None:
        """按住语音按钮 → 开始录音。"""
        if self.is_ai_busy or self.is_tts_playing:
            return
        self.asr_manager.start_recording()

    def _on_voice_button_released(self) -> None:
        """松开语音按钮 → 停止录音，自动触发识别。"""
        if self.asr_manager.is_recording():
            self.asr_manager.stop_recording()

    def _on_recording_started(self) -> None:
        """ASR 录音开始 → 切 LISTENING 表情 + 录音中状态。"""
        self._set_interaction_state("recording")
        self.apply_emotion_decision(
            self.emotion_scheduler.on_user_input("语音输入"),
            schedule_return=False,
        )
        # 更新按钮文字提示
        self.voice_button.setText("🎤 松手发送")

    def _on_recognition_started(self) -> None:
        """Whisper 识别开始 → 切 THINKING 表情。"""
        self._set_interaction_state("thinking")
        self.apply_emotion_decision(
            self.emotion_scheduler.on_ai_start(),
            schedule_return=False,
        )
        self.voice_button.setText("🎤 识别中…")
        self.voice_button.setEnabled(False)

    def _on_voice_text_ready(self, text: str) -> None:
        """语音识别完成 → 把文字喂给 _on_send()。

        复用现有的文字对话流程，不做任何特殊处理。
        """
        # 恢复按钮
        self.voice_button.setText("🎤 按住说话")
        self.voice_button.setEnabled(True)

        # 如果识别为空，静默回到 idle
        if not text or not text.strip():
            self._set_interaction_state("idle")
            self.apply_emotion_decision(
                self.emotion_scheduler.default_decision(
                    reason="语音识别为空，回到待机"
                ),
                schedule_return=False,
            )
            return

        # 把识别文本喂给现有发送流程
        self._on_send(text)

    def _on_voice_error(self, error_msg: str) -> None:
        """语音识别出错 → 恢复 idle。"""
        self.voice_button.setText("🎤 按住说话")
        self.voice_button.setEnabled(True)
        self._set_interaction_state("idle")
        self.chat_output.setText(f"语音识别失败：{error_msg[:60]}")

    # ═══════════════════════════════════════════════════════════════════
    # 🎵 TTS 播放状态处理（新增）
    # ═══════════════════════════════════════════════════════════════════

    def _on_tts_started(self) -> None:
        """TTS 开始播放 → 切 TALKING 表情。"""
        self.is_tts_playing = True
        if self._request_started_at:
            logger.info(
                "voice_latency asr_text_to_first_playback=%.3fs",
                time.monotonic() - self._request_started_at,
            )
        self._tts_pending = False
        self._set_interaction_state("talking")
        # 使用 AI 返回的情感标签，不再硬编码 LISTENING
        self.set_emotion(self._tts_emotion)
        # 文字和语音在同一瞬间出现
        # ???? _on_ai_response ????????????

    def _on_tts_finished(self) -> None:
        """TTS 播放结束 → 恢复 idle。"""
        self.is_tts_playing = False
        self._tts_pending = False
        self._active_stream_session = None
        self._stream_tts_started = False
        self.voice_button.setEnabled(True)
        self._set_interaction_state("idle")
        decision = self.emotion_scheduler.default_decision(
            reason="TTS 播放结束，回到待机", skip_night=True
        )
        self.apply_emotion_decision(decision, schedule_return=False)

    def _on_tts_error(self, error_msg: str) -> None:
        """TTS 播放出错 → 静默恢复 idle。"""
        self.is_tts_playing = False
        self._tts_pending = False
        self._active_stream_session = None
        self._stream_tts_started = False
        self.voice_button.setEnabled(True)
        self._set_interaction_state("idle")
        # TTS ?????????????
        if self._tts_text:
            self.chat_output.setText(self._tts_text)
        # TTS ??????????
        decision = self.emotion_scheduler.default_decision(
            reason="TTS ?????????", skip_night=True
        )
        self.apply_emotion_decision(decision, schedule_return=False)
        # ?????????
        print(f"[TTS ??] {error_msg}")
    def mousePressEvent(self, event) -> None:
        """鼠标按下：记录拖拽偏移（左键）或弹出菜单（右键）。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        """鼠标移动：拖拽窗口跟随移动。"""
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        """鼠标释放：结束拖拽。"""
        self.drag_offset = None
        event.accept()

    def contextMenuEvent(self, event) -> None:
        """右键菜单事件。"""
        self._show_context_menu(event.globalPos())

    def _show_context_menu(self, pos) -> None:
        """Show the fixed mode name and quit action."""
        menu = QMenu(self)
        mode_action = menu.addAction(self.character_manager.get_display_name())
        mode_action.setEnabled(False)
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        chosen = menu.exec(pos)
        if chosen == quit_action:
            QApplication.quit()

    def _configure_tts_for_mode(self) -> None:
        """Configure the selected role's voice without silently changing identity."""
        profile = self.mode_profile
        tts_config = resolve_tts_config(profile)
        if tts_config.get("engine") == "qwen":
            self.tts_manager.set_engine(QwenTTSEngine(
                api_url=tts_config.get("api_url", "http://127.0.0.1:9891"),
                timeout=float(tts_config.get("timeout_seconds", 180)),
                voice_id=str(tts_config.get("voice", "qwen-user-20261028")),
                runtime=tts_config.get("qwen_runtime", "baseline"),
            ))
            print(f"[DesktopPet] TTS: Qwen3-TTS clone for {self.mode_id}")
            return
        voice_key = tts_config.get(
            "fallback_voice", tts_config.get("voice", "yunxi")
        )
        voice_id = EDGE_VOICES.get(voice_key, voice_key)
        edge_fallback = EdgeTTSEngine(voice=voice_id)
        if tts_config.get("engine") == "voxcpm":
            primary = VoxCPMTTSEngine(
                api_url=tts_config.get("api_url", "http://127.0.0.1:9890"),
                inference_steps=int(tts_config.get("inference_steps", 4)),
                timeout=float(tts_config.get("timeout_seconds", 120)),
                voice_id=str(tts_config.get("voice", "laoshaozi-personal")),
                adaptive_steps=tuple(
                    int(step)
                    for step in tts_config.get(
                        "adaptive_inference_steps",
                        [int(tts_config.get("inference_steps", 4))],
                    )
                ),
                buffer_rtf_threshold=float(
                    tts_config.get("buffer_rtf_threshold", 0.95)
                ),
            )
            if bool(tts_config.get("allow_voice_fallback", True)):
                self.tts_manager.set_engine(
                    FallbackTTSEngine(primary, edge_fallback)
                )
                print(
                    f"[DesktopPet] TTS primary: VoxCPM2; fallback: {voice_id} "
                    f"for {self.mode_id}"
                )
            else:
                self.tts_manager.set_engine(primary)
                print(f"[DesktopPet] TTS: VoxCPM2 clone only for {self.mode_id}")
            return
        self.tts_manager.set_engine(edge_fallback)
        print(f"[DesktopPet] TTS: {voice_id} for {self.mode_id}")
