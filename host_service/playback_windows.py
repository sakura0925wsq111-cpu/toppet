"""Known-length PCM playback units for the existing v2 byte-range receiver.

Text sentences and playback units are separate: a unit may span text boundaries.
Every ready record describes exactly the PCM sent next, never a guessed length.
"""
from __future__ import annotations

from collections import deque

from audio.tts.voxcpm_engine import AudioChunk
from .subtitle import build_response_ui_event


DEFAULT_WINDOW_BYTES = 25600  # 800ms at 16kHz mono s16le
DEFAULT_INITIAL_WINDOW_BYTES = 40960  # 1.28s, then 800ms units
MIN_WINDOW_BYTES = 20480     # 640ms; bounds ready descriptors in the 256KiB ring
MAX_PACKET_BYTES = 4096     # comfortably inside existing firmware receive buffer


def validate_window_bytes(window_bytes):
    if (isinstance(window_bytes, bool) or not isinstance(window_bytes, int)
            or not MIN_WINDOW_BYTES <= window_bytes <= 128000 or window_bytes % 640):
        raise ValueError("playback_window_bytes must be a 640-byte multiple between 20480 and 128000")


class PlaybackWindowWriter:
    def __init__(self, *, window_bytes, emit, send_audio, check_cancel, on_first_audio, initial_window_bytes=None):
        validate_window_bytes(window_bytes)
        initial_window_bytes = window_bytes if initial_window_bytes is None else initial_window_bytes
        validate_window_bytes(initial_window_bytes)
        if initial_window_bytes < window_bytes:
            raise ValueError("initial playback unit must be at least the steady window size")
        self.window_bytes = window_bytes
        self.initial_window_bytes = initial_window_bytes
        self.emit = emit
        self.send_audio = send_audio
        self.check_cancel = check_cancel
        self.on_first_audio = on_first_audio
        self.items = deque()
        self.buffered_bytes = 0
        self.sent_bytes = 0
        self.sequence = 0
        self.sentence_start_sequence = 0
        self.first_audio = True

    def begin_sentence(self, text, emotion):
        self.check_cancel()
        self.items.append(("start", (text, emotion)))

    def end_sentence(self, text):
        self.check_cancel()
        self.items.append(("end", text))

    async def write(self, pcm):
        self.check_cancel()
        if len(pcm) % 2:
            raise ValueError("PCM ends on a partial sample")
        if pcm:
            self.items.append(("pcm", pcm))
            self.buffered_bytes += len(pcm)
        target = self.initial_window_bytes if self.first_audio else self.window_bytes
        while self.buffered_bytes >= target:
            await self._drain(target, final=False)
            target = self.window_bytes

    async def _control(self, kind, payload):
        self.check_cancel()
        if kind == "start":
            text, emotion = payload
            self.sentence_start_sequence = self.sequence
            await self.emit("tts_sentence_start", text=text, sequence=self.sequence)
            await self.emit("response_ui", **build_response_ui_event(text, emotion, self.sequence))
        else:
            await self.emit("tts_sentence_end", text=payload,
                            start_sequence=self.sentence_start_sequence,
                            end_sequence=max(self.sentence_start_sequence, self.sequence - 1))

    async def _leading_controls(self, *, ends_only=False):
        while self.items and self.items[0][0] != "pcm":
            if ends_only and self.items[0][0] != "end":
                break
            await self._control(*self.items.popleft())

    async def _drain(self, count, *, final):
        self.check_cancel()
        await self._leading_controls()
        await self.emit("tts_sentence_ready", sequence=self.sequence, bytes=count,
                        unit="pcm_window", stream_offset=self.sent_bytes)
        remaining = count
        while remaining:
            self.check_cancel()
            await self._leading_controls()
            kind, pcm = self.items.popleft()
            assert kind == "pcm"
            piece = pcm[:min(remaining, MAX_PACKET_BYTES)]
            if len(piece) < len(pcm):
                self.items.appendleft(("pcm", pcm[len(piece):]))
            last = final and len(piece) == remaining
            chunk = AudioChunk(piece, 16000, 1, 16, self.sequence, last)
            if self.first_audio:
                self.on_first_audio(self.sequence)
                self.first_audio = False
            if self.send_audio is not None:
                await self.send_audio(chunk)
            self.sent_bytes += len(piece)
            self.buffered_bytes -= len(piece)
            self.sequence += 1
            remaining -= len(piece)
        await self._leading_controls(ends_only=True)

    async def finish(self):
        self.check_cancel()
        if self.buffered_bytes:
            await self._drain(self.buffered_bytes, final=True)
        # A sentence ending exactly at a full-window boundary has no PCM tail.
        await self._leading_controls(ends_only=True)
        if self.items:
            raise RuntimeError("unconsumed sentence controls without audio")
