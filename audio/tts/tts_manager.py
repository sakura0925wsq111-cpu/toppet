# -*- coding: utf-8 -*-
"""Ordered streaming TTS synthesis and playback for the desktop pet."""

from __future__ import annotations

import logging
import os
import queue
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .base_tts import TTSEngine
from .edge_tts_engine import EdgeTTSEngine

logger = logging.getLogger(__name__)

AudioPlayer = Callable[[bytes, str, threading.Event], None]


def _play_with_pygame(
    audio_bytes: bytes,
    audio_format: str,
    stop_event: threading.Event,
) -> None:
    """Play one encoded audio segment and always remove its temporary file."""
    import pygame

    fd, path = tempfile.mkstemp(suffix=f".{audio_format}")
    descriptor_open = True
    try:
        os.write(fd, audio_bytes)
        os.close(fd)
        descriptor_open = False

        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=24000, size=-16, channels=2)
        except Exception:
            pygame.mixer.init()

        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if stop_event.is_set():
                pygame.mixer.music.stop()
                return
            time.sleep(0.05)
    finally:
        if descriptor_open:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(path)
        except OSError:
            pass


class TTSWorker(QThread):
    """Compatibility worker for callers that still need one-shot playback."""

    playback_started = pyqtSignal()
    playback_finished = pyqtSignal()
    playback_error = pyqtSignal(str)

    def __init__(self, engine: TTSEngine, text: str, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._text = text
        self._stop_event = threading.Event()

    def stop_playback(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            audio = self._engine.synthesize(self._text)
            if self._stop_event.is_set() or not audio:
                return
            self.playback_started.emit()
            _play_with_pygame(
                audio, self._engine.audio_format, self._stop_event
            )
            if not self._stop_event.is_set():
                self.playback_finished.emit()
        except Exception as exc:
            if not self._stop_event.is_set():
                self.playback_error.emit(str(exc))


class _StreamingTTSSession:
    """Two-stage pipeline: ordered synthesis producer + playback consumer."""

    _END = object()

    def __init__(
        self,
        session_id: Any,
        engine: TTSEngine,
        synthesis_lock: threading.Lock,
        audio_player: AudioPlayer,
        on_started: Callable[["_StreamingTTSSession"], None],
        on_finished: Callable[["_StreamingTTSSession"], None],
        on_error: Callable[["_StreamingTTSSession", str], None],
        on_first_synthesized: Callable[["_StreamingTTSSession"], None],
    ):
        self.session_id = session_id
        self.engine = engine
        self.synthesis_lock = synthesis_lock
        self.audio_player = audio_player
        self.on_started = on_started
        self.on_finished = on_finished
        self.on_error = on_error
        self.on_first_synthesized = on_first_synthesized
        self.started_at = time.monotonic()

        self.stop_event = threading.Event()
        self._text_queue: queue.Queue[Any] = queue.Queue()
        self._audio_queue: queue.Queue[Any] = queue.Queue()
        self._state_lock = threading.Lock()
        self._end_requested = False
        self._sequence = 0
        self._first_synthesized = False
        self._played_any = False
        self._errors: list[str] = []

        self._synthesis_thread = threading.Thread(
            target=self._synthesis_loop,
            name=f"tts-synthesis-{session_id}",
            daemon=True,
        )
        self._playback_thread = threading.Thread(
            target=self._playback_loop,
            name=f"tts-playback-{session_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._synthesis_thread.start()
        self._playback_thread.start()

    @property
    def active(self) -> bool:
        return self._synthesis_thread.is_alive() or self._playback_thread.is_alive()

    def enqueue(self, text: str) -> bool:
        if not text or not text.strip() or self.stop_event.is_set():
            return False
        with self._state_lock:
            if self._end_requested:
                return False
            sequence = self._sequence
            self._sequence += 1
        self._text_queue.put((sequence, text))
        return True

    def end(self) -> None:
        with self._state_lock:
            if self._end_requested:
                return
            self._end_requested = True
        self._text_queue.put(self._END)

    def cancel(self) -> None:
        self.stop_event.set()
        self._text_queue.put(self._END)
        self._audio_queue.put(self._END)

    def join(self, timeout: float = 0.5) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in (self._synthesis_thread, self._playback_thread):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def _synthesis_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                item = self._text_queue.get()
                if item is self._END:
                    break
                sequence, text = item
                try:
                    with self.synthesis_lock:
                        audio = self.engine.synthesize(text)
                    if self.stop_event.is_set():
                        break
                    if not audio:
                        raise RuntimeError("TTS 合成为空")
                    if not self._first_synthesized:
                        self._first_synthesized = True
                        self.on_first_synthesized(self)
                    self._audio_queue.put(
                        (sequence, audio, self.engine.audio_format)
                    )
                except Exception as exc:
                    message = f"第 {sequence + 1} 段 TTS 失败: {exc}"
                    self._errors.append(message)
                    logger.exception(message)
                    # Keep consuming later sentences; one bad chunk should not
                    # destroy the whole spoken response.
        finally:
            self._audio_queue.put(self._END)

    def _playback_loop(self) -> None:
        emitted_started = False
        while not self.stop_event.is_set():
            item = self._audio_queue.get()
            if item is self._END:
                break
            _sequence, audio, audio_format = item
            try:
                if not emitted_started:
                    emitted_started = True
                    self.on_started(self)
                self.audio_player(audio, audio_format, self.stop_event)
                if self.stop_event.is_set():
                    break
                self._played_any = True
            except Exception as exc:
                message = f"TTS 播放失败: {exc}"
                self._errors.append(message)
                logger.exception(message)

        if self.stop_event.is_set():
            return
        if not self._played_any and self._errors:
            self.on_error(self, self._errors[-1])
            return
        self.on_finished(self)


class TTSManager(QObject):
    """Manage cancellable ordered TTS stream sessions."""

    playback_started = pyqtSignal()
    playback_finished = pyqtSignal()
    playback_error = pyqtSignal(str)

    def __init__(
        self,
        engine: TTSEngine | None = None,
        parent=None,
        audio_player: AudioPlayer | None = None,
    ):
        super().__init__(parent)
        self._engine = engine if engine is not None else EdgeTTSEngine()
        self._uses_pygame = audio_player is None
        self._audio_player = audio_player or _play_with_pygame
        self._lock = threading.RLock()
        self._synthesis_lock = threading.Lock()
        self._session: _StreamingTTSSession | None = None
        self._single_counter = 0

    def set_engine(self, engine: TTSEngine) -> None:
        """Use a new engine for future sessions; active audio is unaffected."""
        with self._lock:
            self._engine = engine

    def begin_stream(self, session_id: Any) -> None:
        self.cancel_stream()
        with self._lock:
            session = _StreamingTTSSession(
                session_id=session_id,
                engine=self._engine,
                synthesis_lock=self._synthesis_lock,
                audio_player=self._audio_player,
                on_started=self._session_started,
                on_finished=self._session_finished,
                on_error=self._session_error,
                on_first_synthesized=self._first_synthesized,
            )
            self._session = session
        session.start()

    def enqueue_text(self, session_id: Any, text: str) -> bool:
        with self._lock:
            session = self._session
        if session is None or session.session_id != session_id:
            return False
        return session.enqueue(text)

    def end_stream(self, session_id: Any) -> None:
        with self._lock:
            session = self._session
        if session is not None and session.session_id == session_id:
            session.end()

    def cancel_stream(self, session_id: Any | None = None) -> None:
        with self._lock:
            session = self._session
            if session is None:
                return
            if session_id is not None and session.session_id != session_id:
                return
            self._session = None
        session.cancel()
        self._stop_active_playback()

    def speak(self, text: str) -> None:
        """Backward-compatible one-shot playback implemented as one stream."""
        if not text or not text.strip():
            return
        with self._lock:
            self._single_counter += 1
            session_id = f"single-{self._single_counter}"
        self.begin_stream(session_id)
        self.enqueue_text(session_id, text)
        self.end_stream(session_id)

    def stop(self) -> None:
        self.cancel_stream()

    def shutdown(self, timeout: float = 0.5) -> None:
        with self._lock:
            session = self._session
            self._session = None
        if session is not None:
            session.cancel()
            self._stop_active_playback()
            session.join(timeout)

    def is_playing(self) -> bool:
        with self._lock:
            return self._session is not None and self._session.active

    @property
    def engine_name(self) -> str:
        with self._lock:
            return self._engine.engine_name

    def _is_current(self, session: _StreamingTTSSession) -> bool:
        with self._lock:
            return self._session is session

    def _stop_active_playback(self) -> None:
        if not self._uses_pygame:
            return
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass

    def _first_synthesized(self, session: _StreamingTTSSession) -> None:
        if self._is_current(session):
            logger.info(
                "voice_latency first_tts_synthesized=%.3fs",
                time.monotonic() - session.started_at,
            )

    def _session_started(self, session: _StreamingTTSSession) -> None:
        if not self._is_current(session):
            return
        logger.info(
            "voice_latency first_playback=%.3fs",
            time.monotonic() - session.started_at,
        )
        self.playback_started.emit()

    def _session_finished(self, session: _StreamingTTSSession) -> None:
        if not self._is_current(session):
            return
        logger.info(
            "voice_latency all_playback_complete=%.3fs",
            time.monotonic() - session.started_at,
        )
        with self._lock:
            if self._session is session:
                self._session = None
        self.playback_finished.emit()

    def _session_error(
        self, session: _StreamingTTSSession, message: str
    ) -> None:
        if not self._is_current(session):
            return
        with self._lock:
            if self._session is session:
                self._session = None
        self.playback_error.emit(message)
