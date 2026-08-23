"""Compare warm CPU Faster Whisper latency and transcript error rate."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.asr.whisper_asr import FasterWhisperEngine


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def normalize(text: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]", "", text.lower())


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        for right_index, right_char in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as audio:
        if (
            audio.getframerate() != 16_000
            or audio.getnchannels() != 1
            or audio.getsampwidth() != 2
        ):
            raise RuntimeError(f"unsupported benchmark WAV: {path}")
        return audio.readframes(audio.getnframes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(r"D:\VoxCPM2\voice_data\laoshaozi\audit\review_manifest.jsonl"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", nargs="+", default=["tiny", "base", "small"])
    parser.add_argument("--clips", type=int, default=10)
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in args.manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][: args.clips]
    results = []
    summary = []
    for model_name in args.models:
        engine = FasterWhisperEngine(
            model_size=model_name,
            device="cpu",
            compute_type="int8",
            language="zh",
        )
        load_started = time.perf_counter()
        engine.preload()
        load_seconds = time.perf_counter() - load_started
        latencies = []
        errors = []
        for row in rows:
            pcm = read_pcm(Path(row["audio"]))
            started = time.perf_counter()
            transcript = engine.transcribe_pcm(pcm)
            elapsed = time.perf_counter() - started
            expected = normalize(row["text_asr"])
            actual = normalize(transcript)
            error_rate = edit_distance(expected, actual) / max(1, len(expected))
            result = {
                "model": model_name,
                "clip_id": row["id"],
                "duration_seconds": row["duration"],
                "latency_seconds": round(elapsed, 4),
                "expected": row["text_asr"],
                "actual": transcript,
                "character_error_rate": round(error_rate, 4),
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
            results.append(result)
            latencies.append(elapsed)
            errors.append(error_rate)
        summary.append({
            "model": model_name,
            "load_seconds": round(load_seconds, 3),
            "runs": len(latencies),
            "latency_p50_seconds": round(statistics.median(latencies), 4),
            "latency_p95_seconds": round(percentile(latencies, 0.95), 4),
            "mean_character_error_rate": round(statistics.mean(errors), 4),
        })
    document = {"summary": summary, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
