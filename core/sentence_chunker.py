# -*- coding: utf-8 -*-
"""Incrementally split streamed model text into TTS-friendly sentences."""

from __future__ import annotations


class SentenceChunker:
    """Collect arbitrary text deltas and emit ordered, lossless chunks.

    Short clauses are held until another terminator arrives.  When a model does
    not emit punctuation for a while, ``max_chars`` provides a bounded latency
    fallback and prefers a natural soft break when one is available.
    """

    TERMINATORS = frozenset("。！？!?；;\n")
    SOFT_BREAKS = frozenset("，,、：: ")

    def __init__(
        self,
        min_chars: int = 6,
        max_chars: int = 52,
        comma_chars: int = 10,
    ):
        if min_chars < 1:
            raise ValueError("min_chars must be positive")
        if max_chars <= min_chars:
            raise ValueError("max_chars must be greater than min_chars")
        if comma_chars < min_chars or comma_chars > max_chars:
            raise ValueError("comma_chars must be between min_chars and max_chars")
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.comma_chars = comma_chars
        self._buffer = ""

    @property
    def buffered_text(self) -> str:
        return self._buffer

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []
        chunks: list[str] = []
        # Process the same prefix at the same time regardless of transport delta
        # boundaries. Otherwise a batched delta can bypass max_chars entirely.
        for char in delta:
            self._buffer += char
            terminator_index = self._find_usable_terminator()
            if terminator_index is not None:
                chunks.append(self._take(terminator_index + 1))
                continue

            soft_index = self._find_usable_soft_break()
            if soft_index is not None:
                chunks.append(self._take(soft_index + 1))
                continue

            if len(self._buffer) >= self.max_chars:
                chunks.append(self._take(self._forced_split_index()))
                continue

        return chunks

    def flush(self) -> list[str]:
        if not self._buffer:
            return []
        return [self._take(len(self._buffer))]

    def reset(self) -> None:
        self._buffer = ""

    def _find_usable_terminator(self) -> int | None:
        for index, char in enumerate(self._buffer):
            if char not in self.TERMINATORS:
                continue
            candidate = self._buffer[: index + 1]
            if self._visible_length(candidate) >= self.min_chars:
                return index
        return None

    def _forced_split_index(self) -> int:
        window = self._buffer[: self.max_chars]
        for index in range(len(window) - 1, self.min_chars - 1, -1):
            if window[index] in self.SOFT_BREAKS:
                return index + 1
        return self.max_chars

    def _find_usable_soft_break(self) -> int | None:
        for index, char in enumerate(self._buffer):
            if char not in self.SOFT_BREAKS:
                continue
            candidate = self._buffer[: index + 1]
            if self._visible_length(candidate) >= self.comma_chars:
                return index
        return None

    def _take(self, count: int) -> str:
        chunk = self._buffer[:count]
        self._buffer = self._buffer[count:]
        return chunk

    @staticmethod
    def _visible_length(text: str) -> int:
        return sum(1 for char in text if not char.isspace())
