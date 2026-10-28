"""Render hardware speech-bubble text into a compact monochrome payload."""

from __future__ import annotations

import base64
import struct
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
COLOR_BLACK = (0, 0, 0)
COLOR_WHITE = (255, 255, 255)
COLOR_BORDER = (132, 130, 132)
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


def _wrap_all_lines(text: str, draw: ImageDraw.ImageDraw) -> list[str]:
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
    return lines


def _wrap_text(text: str, draw: ImageDraw.ImageDraw) -> list[str]:
    font = _font()
    lines = _wrap_all_lines(text, draw)
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > TEXT_MAX_WIDTH:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def paginate_subtitle_text(text: str) -> list[str]:
    """Fit every character into readable two-line pages; never add ellipses."""
    remaining = " ".join(str(text).replace("\r", " ").splitlines()).strip()
    draw = ImageDraw.Draw(Image.new("1", (1, 1)))
    pages = []
    while remaining:
        low, high = 1, len(remaining)
        while low < high:
            middle = (low + high + 1) // 2
            if len(_wrap_all_lines(remaining[:middle], draw)) <= MAX_LINES:
                low = middle
            else:
                high = middle - 1
        end = low
        if end < len(remaining):
            # Prefer a nearby phrase boundary without creating tiny pages.
            candidates = [i + 1 for i, character in enumerate(remaining[:end]) if character in "，。！？；,.!?; "]
            suitable = [i for i in candidates if i >= max(1, int(end * .55))]
            if suitable:
                end = suitable[-1]
        pages.append(remaining[:end])
        remaining = remaining[end:]
    return pages


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


def _rgb565(color: tuple[int, int, int]) -> int:
    red, green, blue = color
    return ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)


def render_speech_bubble_rgb565(text: str) -> bytes:
    """Render the final board-ready 480x140 RGB565 speech-bubble region."""
    text_mask = Image.frombytes(
        "1", (SCREEN_WIDTH, MASK_HEIGHT), render_subtitle_mask(text)
    )
    image = Image.new("RGB", (SCREEN_WIDTH, MASK_HEIGHT), COLOR_BLACK)
    bounds = text_mask.getbbox()
    if bounds is not None:
        left, top, right, bottom = bounds
        bubble_left = max(12, left - 17)
        bubble_top = max(12, top - 11)
        bubble_right = min(SCREEN_WIDTH - 13, right + 17)
        bubble_bottom = min(MASK_HEIGHT - 24, bottom + 11)
        tail_x = SCREEN_WIDTH // 2
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (bubble_left, bubble_top, bubble_right, bubble_bottom),
            radius=16,
            fill=COLOR_BORDER,
        )
        draw.polygon(
            (
                (tail_x - 13, bubble_bottom - 2),
                (tail_x + 13, bubble_bottom - 2),
                (tail_x, min(MASK_HEIGHT - 2, bubble_bottom + 20)),
            ),
            fill=COLOR_BORDER,
        )
        draw.rounded_rectangle(
            (
                bubble_left + 2,
                bubble_top + 2,
                bubble_right - 2,
                bubble_bottom - 2,
            ),
            radius=14,
            fill=COLOR_WHITE,
        )
        draw.polygon(
            (
                (tail_x - 9, bubble_bottom - 3),
                (tail_x + 9, bubble_bottom - 3),
                (tail_x, min(MASK_HEIGHT - 5, bubble_bottom + 15)),
            ),
            fill=COLOR_WHITE,
        )
        image.paste(Image.new("RGB", image.size, COLOR_BLACK), (0, 0), text_mask)
    pixels = bytearray()
    for color in image.getdata():
        pixels.extend(struct.pack("<H", _rgb565(color)))
    return bytes(pixels)


def encode_rgb565_runs(payload: bytes) -> bytes:
    if len(payload) % 2:
        raise ValueError("RGB565 payload ends on a partial pixel")
    colors = memoryview(payload).cast("H")
    encoded = bytearray()
    offset = 0
    while offset < len(colors):
        color = int(colors[offset])
        end = offset + 1
        while end < len(colors) and colors[end] == color and end - offset < 0xFFFF:
            end += 1
        encoded.extend(struct.pack("<HH", end - offset, color))
        offset = end
    return bytes(encoded)


def build_response_ui_event(text: str, emotion: str, sequence: int) -> dict[str, object]:
    encoded = encode_rgb565_runs(render_speech_bubble_rgb565(text))
    return {
        "emotion": str(emotion or "NEUTRAL"),
        "sequence": max(0, int(sequence)),
        "encoding": "rgb565-rle-base64",
        "width": SCREEN_WIDTH,
        "height": MASK_HEIGHT,
        "data": base64.b64encode(encoded).decode("ascii"),
    }
