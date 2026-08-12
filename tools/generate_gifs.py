import json
from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSET_ROOT = PROJECT_ROOT / "assets" / "characters" / "personal"
OUT_ROOT = PROJECT_ROOT / "artifacts" / "generated_gifs"
FRAMES_ROOT = OUT_ROOT / "frames"
GIFS_ROOT = OUT_ROOT / "gifs"
FRAME_DURATION_MS = 150

try:
    RESAMPLE_NEAREST = Image.Resampling.NEAREST
except AttributeError:
    RESAMPLE_NEAREST = Image.NEAREST


PROFILES = {
    "ANGRY": [(-5, 0, -2.0, 1.00, 1.00), (5, -1, 2.0, 1.00, 1.00), (-4, 0, -1.5, 1.00, 1.00), (4, -1, 1.5, 1.00, 1.00)],
    "CELEBRATE": [(0, 0, 0, 1.00, 1.00), (0, -10, -1, 1.02, 1.02), (0, -4, 1, 1.00, 1.00), (0, -8, 0, 1.01, 1.01)],
    "COOL": [(0, 0, 0, 1.00, 1.00), (0, -2, -0.5, 1.01, 1.00), (0, -3, 0.5, 1.01, 1.00), (0, -1, 0, 1.00, 1.00)],
    "DRINKING": [(0, 0, 0, 1.00, 1.00), (-1, -2, -1.0, 1.00, 1.00), (-2, -3, -2.0, 1.00, 1.00), (-1, -1, -1.0, 1.00, 1.00)],
    "EATING": [(0, 0, 0, 1.00, 1.00), (0, 1, 0, 1.02, 0.98), (0, -1, 0, 0.99, 1.02), (0, 0, 0, 1.01, 0.99)],
    "GOOD_NIGHT": [(0, 1, -0.7, 1.00, 1.00), (0, 3, 0.4, 0.99, 1.00), (0, 4, 0.8, 0.99, 1.00), (0, 2, 0.2, 1.00, 1.00)],
    "GREETING": [(0, 0, 0, 1.00, 1.00), (2, -5, 1.0, 1.01, 1.01), (-1, -3, -1.0, 1.00, 1.00), (2, -5, 1.0, 1.01, 1.01)],
    "HAPPY": [(0, 0, 0, 1.00, 1.00), (0, -6, 0, 1.02, 1.02), (0, -2, 0, 1.00, 1.00), (0, -5, 0, 1.01, 1.01)],
    "HEART": [(0, 0, 0, 1.00, 1.00), (0, -4, -0.5, 1.02, 1.02), (0, -2, 0.5, 1.01, 1.01), (0, -5, 0, 1.02, 1.02)],
    "LISTENING": [(0, 0, 0, 1.00, 1.00), (0, -2, -0.5, 1.01, 1.00), (0, -3, 0.5, 1.01, 1.00), (0, -1, 0, 1.00, 1.00)],
    "LOVE": [(0, 0, 0, 1.00, 1.00), (0, -5, -0.5, 1.02, 1.02), (0, -2, 0.5, 1.01, 1.01), (0, -5, 0, 1.02, 1.02)],
    "NEUTRAL": [(0, 0, 0, 1.00, 1.00), (0, -2, 0, 1.01, 1.01), (0, -3, 0, 1.00, 1.00), (0, -1, 0, 0.995, 0.995)],
    "READING": [(0, 0, 0, 1.00, 1.00), (0, -1, -0.3, 1.00, 1.00), (0, -2, 0.3, 1.00, 1.00), (0, -1, 0, 1.00, 1.00)],
    "SAD": [(0, 1, -0.4, 0.995, 1.00), (0, 3, 0.2, 0.99, 1.00), (0, 4, 0.4, 1.00, 1.00), (0, 2, 0.0, 0.995, 1.00)],
    "SHY": [(-1, 0, -0.5, 1.00, 1.00), (1, -2, 0.5, 1.01, 1.01), (0, -1, 0, 1.00, 1.00), (1, -2, 0.5, 1.01, 1.01)],
    "SLEEPY": [(0, 1, -0.7, 1.00, 1.00), (0, 3, 0.4, 0.99, 1.00), (0, 4, 0.8, 0.99, 1.00), (0, 2, 0.2, 1.00, 1.00)],
    "SURPRISED": [(0, 0, 0, 1.00, 1.00), (0, -5, 0, 1.05, 1.05), (0, -2, 0, 1.01, 1.01), (0, 0, 0, 0.99, 0.99)],
    "THINKING": [(0, 0, 0, 1.00, 1.00), (0, -2, -0.4, 1.01, 1.00), (0, -3, 0.4, 1.01, 1.00), (0, -1, 0, 1.00, 1.00)],
    "TIRED": [(0, 2, -0.5, 0.995, 1.00), (0, 4, 0.3, 0.99, 1.00), (0, 5, 0.6, 0.99, 1.00), (0, 3, 0.0, 0.995, 1.00)],
    "WORKING": [(0, 0, 0, 1.00, 1.00), (0, -2, -0.3, 1.00, 1.00), (0, -1, 0.3, 1.00, 1.00), (0, -2, 0, 1.00, 1.00)],
}


def transform_frame(base, dx, dy, angle, sx, sy):
    width, height = base.size
    transformed = base.resize(
        (max(1, round(width * sx)), max(1, round(height * sy))),
        RESAMPLE_NEAREST,
    )
    if angle:
        transformed = transformed.rotate(angle, resample=RESAMPLE_NEAREST, expand=True)

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(
        transformed,
        ((width - transformed.width) // 2 + dx, (height - transformed.height) // 2 + dy),
    )
    return canvas


def rgba_to_gif_palette(frame):
    alpha = frame.getchannel("A")
    white_backed = Image.new("RGBA", frame.size, (255, 255, 255, 255))
    white_backed.alpha_composite(frame)
    paletted = white_backed.convert("P", palette=Image.ADAPTIVE, colors=255)
    transparent_index = 255
    transparent_mask = alpha.point(lambda value: 255 if value <= 8 else 0)
    paletted.paste(transparent_index, transparent_mask)

    palette = paletted.getpalette() or []
    if len(palette) < 768:
        palette += [0] * (768 - len(palette))
    palette[transparent_index * 3 : transparent_index * 3 + 3] = [0, 0, 0]
    paletted.putpalette(palette)
    paletted.info["transparency"] = transparent_index
    paletted.info["disposal"] = 2
    return paletted


def main():
    png_files = sorted(
        path for path in ASSET_ROOT.iterdir() if path.is_file() and path.suffix.lower() == ".png"
    )
    FRAMES_ROOT.mkdir(parents=True, exist_ok=True)
    GIFS_ROOT.mkdir(parents=True, exist_ok=True)

    manifest = []
    for source_path in png_files:
        name = source_path.stem
        base = Image.open(source_path).convert("RGBA")
        frame_dir = FRAMES_ROOT / name
        frame_dir.mkdir(parents=True, exist_ok=True)

        frames = []
        for frame_index, params in enumerate(PROFILES.get(name, PROFILES["NEUTRAL"]), start=1):
            frame = transform_frame(base, *params)
            frame_path = frame_dir / f"{name}_{frame_index:02d}.png"
            frame.save(frame_path)
            frames.append(frame)

        gif_frames = [rgba_to_gif_palette(frame) for frame in frames]
        gif_path = GIFS_ROOT / f"{name}.gif"
        gif_frames[0].save(
            gif_path,
            save_all=True,
            append_images=gif_frames[1:],
            duration=FRAME_DURATION_MS,
            loop=0,
            transparency=255,
            disposal=2,
            optimize=False,
        )

        manifest.append(
            {
                "source": source_path.relative_to(PROJECT_ROOT).as_posix(),
                "frames_dir": frame_dir.relative_to(PROJECT_ROOT).as_posix(),
                "gif": gif_path.relative_to(PROJECT_ROOT).as_posix(),
                "frames": len(frames),
                "duration_ms": FRAME_DURATION_MS,
                "size": list(base.size),
            }
        )

    (OUT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Generated {len(manifest)} GIFs and {sum(item['frames'] for item in manifest)} PNG frames.")


if __name__ == "__main__":
    main()
