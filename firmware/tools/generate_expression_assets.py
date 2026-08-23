"""Build LittleFS expression assets and Chinese speech-bubble masks."""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SCREEN_WIDTH = 480
SCREEN_HEIGHT = 480
PET_WIDTH = 420
PET_HEIGHT = 340
TEXT_MASK_HEIGHT = 140
MAX_PALETTE_COLORS = 256
TEXT_MAX_WIDTH = 260
TEXT_FONT_SIZE = 25
TEXT_LINE_HEIGHT = 32

ASSET_HEADER = struct.Struct("<4sHHHHIIII")
FRAME_ENTRY = struct.Struct("<IIHH")
ASSET_MAGIC = b"TPA1"
ASSET_VERSION = 1

EXPRESSIONS = {
    "NEUTRAL": "我在这里，随时陪你。",
    "HAPPY": "今天也要开开心心！",
    "GREETING": "你好呀，见到你真开心！",
    "LISTENING": "我在听，你慢慢说。",
    "THINKING": "让我认真想一想。",
    "LOVE": "最喜欢和你待在一起。",
    "HEART": "送你一颗小心心。",
    "CELEBRATE": "太棒啦，庆祝一下！",
    "COOL": "放心，交给我吧。",
    "SURPRISED": "哇，真的吗？",
    "SHY": "你这样说，我会害羞的。",
    "ANGRY": "哼，我现在有点生气！",
    "SAD": "我有一点点难过……",
    "TIRED": "今天有点累了……",
    "SLEEPY": "好困呀，让我眯一会儿。",
    "GOOD_NIGHT": "晚安，做个好梦。",
    "EATING": "我要开始吃饭啦！",
    "DRINKING": "先喝口水，休息一下。",
    "READING": "让我认真读一读。",
    "WORKING": "我正在努力工作！",
}


def rgb565(red: int, green: int, blue: int) -> int:
    return ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)


def encode_runs(indices: bytes) -> bytes:
    if not indices:
        return b""
    encoded = bytearray()
    color = indices[0]
    length = 1

    def emit(run_length: int, run_color: int) -> None:
        while run_length:
            chunk = min(run_length, 0xFFFF)
            encoded.extend((chunk & 0xFF, chunk >> 8, run_color))
            run_length -= chunk

    for next_color in indices[1:]:
        if next_color == color and length < 0xFFFF:
            length += 1
        else:
            emit(length, color)
            color = next_color
            length = 1
    emit(length, color)
    return bytes(encoded)


def union_alpha_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    bbox: tuple[int, int, int, int] | None = None
    for frame_index in range(getattr(image, "n_frames", 1)):
        image.seek(frame_index)
        frame_bbox = image.convert("RGBA").getchannel("A").getbbox()
        if frame_bbox is None:
            continue
        if bbox is None:
            bbox = frame_bbox
        else:
            bbox = (
                min(bbox[0], frame_bbox[0]),
                min(bbox[1], frame_bbox[1]),
                max(bbox[2], frame_bbox[2]),
                max(bbox[3], frame_bbox[3]),
            )
    if bbox is None:
        raise ValueError("Animation contains no visible pixels")
    return bbox


def prepare_frames(source: Path) -> tuple[list[Image.Image], list[int]]:
    image = Image.open(source)
    bbox = union_alpha_bbox(image)
    source_width = bbox[2] - bbox[0]
    source_height = bbox[3] - bbox[1]
    scale = min((PET_WIDTH - 8) / source_width, (PET_HEIGHT - 8) / source_height)
    target_width = max(1, round(source_width * scale))
    target_height = max(1, round(source_height * scale))
    target_x = (PET_WIDTH - target_width) // 2
    target_y = PET_HEIGHT - target_height

    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame_index in range(getattr(image, "n_frames", 1)):
        image.seek(frame_index)
        rgba = image.convert("RGBA").crop(bbox).resize(
            (target_width, target_height), Image.Resampling.NEAREST
        )
        canvas = Image.new("RGBA", (PET_WIDTH, PET_HEIGHT), (0, 0, 0, 255))
        canvas.alpha_composite(rgba, (target_x, target_y))
        frames.append(canvas.convert("RGB"))
        durations.append(max(30, int(image.info.get("duration", 150) or 150)))
    return frames, durations


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> list[str]:
    if draw.textlength(text, font=font) <= TEXT_MAX_WIDTH:
        return [text]

    candidates: list[tuple[float, str, str]] = []
    leading_punctuation = "，。！？；：、）》】」』…"
    trailing_punctuation = "《【「『"
    for split_at in range(1, len(text)):
        first = text[:split_at]
        second = text[split_at:]
        first_width = draw.textlength(first, font=font)
        second_width = draw.textlength(second, font=font)
        if first_width > TEXT_MAX_WIDTH or second_width > TEXT_MAX_WIDTH:
            continue
        if second[0] in leading_punctuation or first[-1] in trailing_punctuation:
            continue
        score = max(first_width, second_width) + abs(first_width - second_width) * 0.35
        candidates.append((score, first, second))
    if candidates:
        _, first, second = min(candidates, key=lambda candidate: candidate[0])
        return [first, second]

    # Extremely long custom text falls back to greedy wrapping and ellipsis.
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        if current and draw.textlength(candidate, font=font) > TEXT_MAX_WIDTH:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > 2:
        lines = lines[:2]
        while draw.textlength(lines[1] + "…", font=font) > TEXT_MAX_WIDTH:
            lines[1] = lines[1][:-1]
        lines[1] += "…"
    return lines


def make_text_mask(text: str, font_path: Path) -> tuple[Image.Image, bytes]:
    mask = Image.new("1", (SCREEN_WIDTH, TEXT_MASK_HEIGHT), 0)
    draw = ImageDraw.Draw(mask)
    font = ImageFont.truetype(str(font_path), TEXT_FONT_SIZE)
    lines = wrap_text(draw, text, font)
    start_y = 31 + (2 - len(lines)) * TEXT_LINE_HEIGHT // 2
    for line_index, line in enumerate(lines):
        line_width = draw.textlength(line, font=font)
        draw.text(
            ((SCREEN_WIDTH - line_width) / 2, start_y + line_index * TEXT_LINE_HEIGHT),
            line,
            font=font,
            fill=1,
            stroke_width=0,
        )
    packed = mask.tobytes()
    expected = SCREEN_WIDTH * TEXT_MASK_HEIGHT // 8
    if len(packed) != expected:
        raise RuntimeError(f"Unexpected text mask size: {len(packed)} != {expected}")
    return mask, packed


def draw_preview(frame: Image.Image, text_mask: Image.Image) -> Image.Image:
    preview = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), "black")
    preview.paste(frame, ((SCREEN_WIDTH - PET_WIDTH) // 2, SCREEN_HEIGHT - PET_HEIGHT))
    draw = ImageDraw.Draw(preview)
    bbox = text_mask.getbbox()
    if bbox is None:
        raise RuntimeError("Subtitle mask is empty")
    padding_x = 17
    padding_y = 11
    bubble = (
        max(12, bbox[0] - padding_x),
        max(12, bbox[1] - padding_y),
        min(SCREEN_WIDTH - 13, bbox[2] + padding_x),
        min(TEXT_MASK_HEIGHT - 25, bbox[3] + padding_y),
    )
    tail_x = SCREEN_WIDTH // 2
    bubble_bottom = bubble[3]
    border = (126, 132, 145)
    draw.rounded_rectangle(bubble, radius=16, fill=border)
    draw.polygon(
        ((tail_x - 13, bubble_bottom - 2),
         (tail_x + 13, bubble_bottom - 2),
         (tail_x, min(TEXT_MASK_HEIGHT - 2, bubble_bottom + 20))),
        fill=border,
    )
    inner = (bubble[0] + 2, bubble[1] + 2, bubble[2] - 2, bubble[3] - 2)
    draw.rounded_rectangle(inner, radius=14, fill="white")
    draw.polygon(
        ((tail_x - 9, bubble_bottom - 3),
         (tail_x + 9, bubble_bottom - 3),
         (tail_x, min(TEXT_MASK_HEIGHT - 5, bubble_bottom + 15))),
        fill="white",
    )
    text_layer = Image.new("RGB", (SCREEN_WIDTH, TEXT_MASK_HEIGHT), "black")
    preview.paste(text_layer, (0, 0), text_mask.convert("L"))
    return preview


def build_asset(
    expression: str,
    subtitle: str,
    source: Path,
    output: Path,
    font_path: Path,
    preview_dir: Path | None,
) -> tuple[int, str]:
    frames, durations = prepare_frames(source)
    strip = Image.new("RGB", (PET_WIDTH, PET_HEIGHT * len(frames)))
    for frame_index, frame in enumerate(frames):
        strip.paste(frame, (0, frame_index * PET_HEIGHT))
    quantized = strip.quantize(
        colors=MAX_PALETTE_COLORS,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )

    palette = (quantized.getpalette() or [])[: MAX_PALETTE_COLORS * 3]
    palette.extend([0] * (MAX_PALETTE_COLORS * 3 - len(palette)))
    palette565 = [
        rgb565(*palette[index : index + 3])
        for index in range(0, MAX_PALETTE_COLORS * 3, 3)
    ]
    palette_bytes = struct.pack("<256H", *palette565)

    encoded_frames = []
    for frame_index in range(len(frames)):
        top = frame_index * PET_HEIGHT
        indices = quantized.crop((0, top, PET_WIDTH, top + PET_HEIGHT)).tobytes()
        encoded_frames.append(encode_runs(indices))

    text_mask_image, text_mask = make_text_mask(subtitle, font_path)
    palette_offset = ASSET_HEADER.size
    frame_table_offset = palette_offset + len(palette_bytes)
    text_mask_offset = frame_table_offset + FRAME_ENTRY.size * len(frames)
    frame_data_offset = text_mask_offset + len(text_mask)

    entries = bytearray()
    current_offset = frame_data_offset
    for encoded, duration in zip(encoded_frames, durations):
        entries.extend(FRAME_ENTRY.pack(current_offset, len(encoded), duration, 0))
        current_offset += len(encoded)

    header = ASSET_HEADER.pack(
        ASSET_MAGIC,
        ASSET_VERSION,
        PET_WIDTH,
        PET_HEIGHT,
        len(frames),
        palette_offset,
        frame_table_offset,
        text_mask_offset,
        len(text_mask),
    )
    payload = header + palette_bytes + entries + text_mask + b"".join(encoded_frames)
    output.write_bytes(payload)

    if preview_dir is not None:
        preview_dir.mkdir(parents=True, exist_ok=True)
        draw_preview(frames[0], text_mask_image).save(preview_dir / f"{expression}.png")
    return len(payload), hashlib.sha256(payload).hexdigest()


def write_manifest(output: Path) -> None:
    names = list(EXPRESSIONS)
    lines = [
        "#pragma once",
        "",
        "#include <Arduino.h>",
        "",
        f"constexpr uint8_t EXPRESSION_COUNT = {len(names)};",
        "static const char *const kExpressionNames[EXPRESSION_COUNT] = {",
        "  " + ", ".join(f'\"{name}\"' for name in names),
        "};",
        "static const char *const kExpressionPaths[EXPRESSION_COUNT] = {",
        "  " + ", ".join(f'\"/{name}.tpa\"' for name in names),
        "};",
        "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=repository_root / "characters/personal/expressions",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repository_root / "firmware/toppet_pet/data",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository_root / "firmware/toppet_pet/expression_manifest.h",
    )
    parser.add_argument(
        "--font",
        type=Path,
        default=Path("C:/Windows/Fonts/msyhbd.ttc"),
    )
    parser.add_argument("--preview-dir", type=Path)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    total_size = 0
    for expression, subtitle in EXPRESSIONS.items():
        source = args.source_dir / f"{expression}.gif"
        if not source.exists():
            raise FileNotFoundError(source)
        size, digest = build_asset(
            expression,
            subtitle,
            source,
            args.output_dir / f"{expression}.tpa",
            args.font,
            args.preview_dir,
        )
        total_size += size
        print(f"{expression:12s} {size:9,d} bytes  SHA256 {digest}")
    write_manifest(args.manifest)
    print(f"Generated {len(EXPRESSIONS)} expressions; total {total_size:,} bytes")


if __name__ == "__main__":
    main()
