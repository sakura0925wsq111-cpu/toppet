"""Manual real-model smoke test; intentionally excluded from pytest discovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QTimer  # noqa: E402

from audio.tts.voxcpm_engine import VoxCPMTTSEngine  # noqa: E402
from systems.voxcpm_service_manager import VoxCPMServiceManager  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--text", default="呆呆宝宝，找我啥事？今天过得怎么样？"
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    app = QCoreApplication([])
    manager = VoxCPMServiceManager(max_wait=150, inference_steps=4)
    result: dict[str, object] = {"ok": False}

    def fail(message: str) -> None:
        result["error"] = message
        manager.stop_service()
        app.exit(1)

    def ready() -> None:
        try:
            engine = VoxCPMTTSEngine(inference_steps=4, timeout=120)
            started = time.perf_counter()
            audio = engine.synthesize(args.text)
            elapsed = time.perf_counter() - started
            args.output.write_bytes(audio)
            with wave.open(str(args.output), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
            metrics = requests.get(
                "http://127.0.0.1:9890/metrics", timeout=3
            ).json()
            result.update(
                {
                    "ok": True,
                    "output": str(args.output.resolve()),
                    "text": args.text,
                    "request_seconds": round(elapsed, 3),
                    "audio_seconds": round(duration, 3),
                    "service_metrics": metrics,
                }
            )
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            manager.stop_service()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            QTimer.singleShot(1500, lambda: app.exit(0 if result["ok"] else 1))

    manager.service_ready.connect(ready)
    manager.service_failed.connect(fail)
    QTimer.singleShot(180_000, lambda: fail("outer smoke-test timeout"))
    manager.start_service()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
