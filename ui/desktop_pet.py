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
import re

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
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

from core.config import PetConfig, api_key_help_text, ASSETS_DIR
from core.pet_brain import PetBrain

from emotion_scheduler import (
    EMOTION_ALIASES,
    EMOTION_ASSETS,
    EmotionDecision,
    PetEmotionScheduler,
)

from ui.ai_worker import AIWorker

# ── 语音模块 ──
from audio import TTSManager, ASRManager


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

    def __init__(self, config: PetConfig, brain: PetBrain):
        super().__init__()
        self.config = config
        self.brain = brain
        self.drag_offset = None

        # ── 检查 API Key ──
        if not self.config.api_key:
            QMessageBox.warning(
                self,
                "缺少 API Key",
                api_key_help_text() + "\n\n时间和天气查询仍可使用。",
            )

        # ── 图片缓存 ──
        self.pixmaps: dict[str, QPixmap] = {}

        # ── 情绪调度器 ──
        self.emotion_scheduler = PetEmotionScheduler()
        self.emotion_return_token = 0
        self.is_ai_busy = False
        self.is_tts_playing = False   # TTS
        self._tts_pending = False    # TTS ??????????
        self._tts_emotion = "NEUTRAL"    # 缓存 AI 情感，TTS 播放时切表情
        self._tts_text = ""              # 缓存 AI 回复文字，与语音同步显示
        self.interaction_state = "idle"

        # ── 语音管理器（TTS + ASR）──
        self.tts_manager = TTSManager()
        self.asr_manager = ASRManager()
        self._connect_voice_signals()

        # ????? ASR ?????????????????
        self.asr_manager.preload()

        # ── 初始化 UI ──
        self._setup_ui()
        self._load_images()

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
        self.setWindowTitle(self.config.name)
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
        self.chat_output = QLabel("呆呆宝宝，找我啥事？")
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
        self.chat_input.setPlaceholderText("和老勺子说点啥…")
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
        """加载桌宠表情图片。"""
        missing: list[str] = []
        for emo in EMOTION_ASSETS:
            pixmap = QPixmap()
            for suffix in (".png", ".jpg", ".jpeg"):
                image_path = os.path.join(ASSETS_DIR, f"{emo}{suffix}")
                candidate = QPixmap(image_path)
                if not candidate.isNull():
                    pixmap = candidate
                    break

            self.pixmaps[emo] = pixmap
            if pixmap.isNull():
                missing.append(f"{emo}.png/.jpg")

        if missing:
            self.chat_output.setText("有些表情图片没找到：" + "、".join(missing))

    def set_emotion(self, emotion: str) -> None:
        """根据传来的情感标签切换宠物图片。"""
        raw_emotion = str(emotion).strip()
        emotion = EMOTION_ALIASES.get(
            raw_emotion,
            re.sub(r"[\s-]+", "_", raw_emotion.upper()),
        )
        pixmap = self.pixmaps.get(emotion) or self.pixmaps.get("NEUTRAL")
        if not pixmap or pixmap.isNull():
            return

        scaled = pixmap.scaled(
            self.pet_label.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.pet_label.setPixmap(scaled)

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
        # ??????
        self.chat_output.setText(f"??{text}")
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

        self.worker = AIWorker(self.brain, text, self)
        self.worker.response_ready.connect(self._on_ai_response)
        self.worker.error_occurred.connect(self._on_ai_error)
        self.worker.start()

    def _on_ai_response(self, data: dict) -> None:
        """AI 成功返回回复时的处理。

        在显示文字后，自动调用 TTS 朗读回复。
        """
        emotion = data.get("emotion", "NEUTRAL")
        content = data.get("content", "呆呆宝宝，我刚刚脑子短路了，再问我一次～")
        user_text = getattr(self.worker, "user_text", "")

        # 恢复界面
        self.is_ai_busy = False

        # 更新表情
        decision = self.emotion_scheduler.on_reply(user_text, content, emotion)
        self.apply_emotion_decision(decision, schedule_return=False)

        # 缓存 AI 回复和情感，等 TTS 播放时再同步显示（文字语音同时出现）
        self._tts_emotion = decision.emotion  # ????????
        self._tts_text = content
        # ????????? TTS????????
        self.chat_output.setText(content)

        # 恢复输入（语音按钮在 TTS 播放结束后恢复）
        self.chat_input.setEnabled(True)
        self.chat_input.setFocus()
        self._set_interaction_state("idle")

        # ── 🎵 新增：自动朗读 AI 回复 ──
        self._tts_pending = True
        self.tts_manager.speak(content)

    def _on_ai_error(self, err: str) -> None:
        """AI 处理失败时的兜底处理。"""
        self.is_ai_busy = False
        self.apply_emotion_decision(self.emotion_scheduler.on_error())
        self.chat_output.setText("人机，我这边处理失败了：" + err[:80])
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
        """显示右键菜单（退出）。"""
        menu = QMenu(self)
        quit_action = menu.addAction("退出老勺子")
        chosen = menu.exec(pos)
        if chosen == quit_action:
            QApplication.quit()
