"""Deterministic model of the ESP32 PCM ring-buffer acceptance policy."""

from __future__ import annotations


class PlaybackRingModel:
    def __init__(
        self,
        capacity: int = 16 * 1024,
        prebuffer: int = 3840,
        low_water: int = 2560,
        high_water: int = 16000,
    ) -> None:
        if not 0 < low_water < prebuffer <= high_water <= capacity:
            raise ValueError("invalid playback watermarks")
        self.capacity = capacity
        self.prebuffer = prebuffer
        self.low_water = low_water
        self.high_water = high_water
        self.session_id: str | None = None
        self.buffer = bytearray()
        self.started = False
        self.finished = False
        self.underruns = 0
        self.high_water_warnings = 0

    def start(self, session_id: str) -> None:
        self.abort()
        self.session_id = session_id

    def push(self, session_id: str, pcm: bytes) -> bool:
        if session_id != self.session_id:
            return False
        if len(self.buffer) + len(pcm) > self.capacity:
            raise BufferError("playback ring overflow requires backpressure")
        self.buffer.extend(pcm)
        if len(self.buffer) > self.high_water:
            self.high_water_warnings += 1
        if not self.started and (
            len(self.buffer) >= self.prebuffer or self.finished
        ):
            self.started = True
        return True

    def mark_finished(self, session_id: str) -> None:
        if session_id == self.session_id:
            self.finished = True
            if self.buffer:
                self.started = True

    def read(self, size: int) -> bytes:
        if not self.started:
            return b""
        if not self.buffer:
            if not self.finished:
                self.underruns += 1
            return b""
        count = min(size, len(self.buffer))
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    @property
    def low(self) -> bool:
        return self.started and not self.finished and len(self.buffer) < self.low_water

    def abort(self) -> None:
        self.session_id = None
        self.buffer.clear()
        self.started = False
        self.finished = False
