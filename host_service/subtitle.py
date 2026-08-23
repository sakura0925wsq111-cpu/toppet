"""Render hardware speech-bubble text into a compact monochrome payload."""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SCREEN_WIDTH = 480
MASK_HEIGHT = 140
TEXT_MAX_WIDTH = 400
FONT_SIZE = 25
LINE_HEIGHT = 32
MAX_LINES = 2
MASK_BYTES = SCREEN_WIDTH * MASK_HEIGHT // 8
FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
)


@lru_cache(maxsize=1)
def _font() -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if path.is_file():
            return ImageFont.truetype(str(path), FONT_SIZE)
    raise RuntimeError("No supported Chinese subtitle font is installed")


def _wrap_text(text: str, draw: ImageDraw.ImageDraw) -> list[str]:
    normalized = " ".join(str(text).replace("\r", " ").splitlines()).strip()
    if not normalized:
        return []
    font = _font()
    lines: list[str] = []
    current = ""
    for character in normalized:
        candidate = current + character
        if current and draw.textlength(candidate, font=font) > TEXT_MAX_WIDTH:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > TEXT_MAX_WIDTH:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def render_subtitle_mask(text: str) -> bytes:
    mask = Image.new("1", (SCREEN_WIDTH, MASK_HEIGHT), 0)
    draw = ImageDraw.Draw(mask)
    font = _font()
    lines = _wrap_text(text, draw)
    start_y = 31 + (MAX_LINES - len(lines)) * LINE_HEIGHT // 2
    for index, line in enumerate(lines):
        width = draw.textlength(line, font=font)
        draw.text(
            ((SCREEN_WIDTH - width) / 2, start_y + index * LINE_HEIGHT),
            line,
            font=font,
            fill=1,
        )
    packed = mask.tobytes()
    if len(packed) != MASK_BYTES:
        raise RuntimeError(f"Unexpected subtitle mask size: {len(packed)}")
    return packed


def encode_zero_literal_rle(payload: bytes) -> bytes:
    """Pack zero runs and non-zero literal runs into one-byte headers."""
    encoded = bytearray()
    offset = 0
    while offset < len(payload):
        if payload[offset] == 0:
            end = offset + 1
            while end < len(payload) and payload[end] == 0 and end - offset < 128:
                end += 1
            encoded.append(0x80 | (end - offset - 1))
        else:
            end = offset + 1
            while end < len(payload) and payload[end] != 0 and end - offset < 128:
                end += 1
            encoded.append(end - offset - 1)
            encoded.extend(payload[offset:end])
        offset = end
    return bytes(encoded)


def build_subtitle_event(text: str) -> dict[str, object]:
    encoded = encode_zero_literal_rle(render_subtitle_mask(text))
    return {
        "encoding": "zero-literal-rle-base64",
        "width": SCREEN_WIDTH,
        "height": MASK_HEIGHT,
        "data": base64.b64encode(encoded).decode("ascii"),
    }
