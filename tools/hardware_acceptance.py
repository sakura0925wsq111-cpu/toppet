"""Drive repeatable acoustic TopPet hardware rounds over the USB test trigger."""

from __future__ import annotations

import argparse
import audioop
import json
import re
import time
import wave
from pathlib import Path

import pyaudio
import serial
from pycaw.pycaw import AudioUtilities


DEFAULT_CLIPS = Path(r"D:\VoxCPM2\voice_data\laoshaozi\audit\review_clips")


def load_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as audio:
        if (
            audio.getframerate() != 16_000
            or audio.getnchannels() != 1
            or audio.getsampwidth() != 2
        ):
            raise RuntimeError(f"unsupported test WAV: {path}")
        return audio.readframes(audio.getnframes())


def make_duration(pcm: bytes, seconds: int) -> bytes:
    wanted = seconds * 16_000 * 2
    repeated = (pcm * (wanted // len(pcm) + 1))[:wanted]
    return repeated


class SerialMonitor:
    def __init__(self, port: str, baud: int):
        self.serial = serial.Serial(port, baud, timeout=0.05)
        self.serial.dtr = False
        self.serial.rts = False

    def close(self):
        self.serial.close()

    def write(self, command: str):
        self.serial.write((command + "\n").encode("ascii"))
        self.serial.flush()

    def read_until(self, patterns: tuple[str, ...], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        output = ""
        while time.monotonic() < deadline:
            data = self.serial.read(self.serial.in_waiting or 1)
            if data:
                text = data.decode("utf-8", "replace")
                output += text
                print(text, end="", flush=True)
                if any(pattern in output for pattern in patterns):
                    return output
        return output


def play_pcm(player: pyaudio.PyAudio, pcm: bytes, gain: float) -> None:
    amplified = audioop.mul(pcm, 2, gain)
    stream = player.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=16_000,
        output=True,
        frames_per_buffer=640,
    )
    try:
        for offset in range(0, len(amplified), 1280):
            stream.write(amplified[offset:offset + 1280])
    finally:
        stream.stop_stream()
        stream.close()


def parse_round(round_number: int, duration: int, output: str) -> dict[str, object]:
    session_match = re.search(r"button_pressed session_id=([^ ]+)", output)
    samples_match = re.search(
        r"capture session=.* samples=(\d+) ms=(\d+) min=(-?\d+) max=(-?\d+) rms=(\d+) frames=(\d+)",
        output,
    )
    nodes = {}
    for match in re.finditer(
        r"\[LATENCY\] node=([^ ]+) session_id=([^ ]*) .*?monotonic_ms=(\d+).*?sequence=(-?\d+) buffer_bytes=(-?\d+)",
        output,
    ):
        nodes[match.group(1)] = {
            "monotonic_ms": int(match.group(3)),
            "sequence": int(match.group(4)),
            "buffer_bytes": int(match.group(5)),
        }
    release = nodes.get("button_released", {}).get("monotonic_ms")
    first_i2s = nodes.get("first_i2s_write", {}).get("monotonic_ms")
    return {
        "round": round_number,
        "requested_audio_seconds": duration,
        "session_id": session_match.group(1) if session_match else None,
        "capture": None if samples_match is None else {
            "samples": int(samples_match.group(1)),
            "milliseconds": int(samples_match.group(2)),
            "minimum": int(samples_match.group(3)),
            "maximum": int(samples_match.group(4)),
            "rms": int(samples_match.group(5)),
            "frames": int(samples_match.group(6)),
        },
        "latency_nodes": nodes,
        "first_sound_ms": (
            first_i2s - release
            if release is not None and first_i2s is not None
            else None
        ),
        "underruns": output.count("playback_underrun"),
        "overflow_warnings": output.count("playback overflow"),
        "sequence_errors": output.count("sequence mismatch"),
        "stale_frames": output.count("stale/invalid"),
        "injection_timeouts": output.count("TEST_PCM TIMEOUT"),
        "completed": "playback finished" in output,
        "crashed": "Guru Meditation" in output or "assert failed" in output,
        "raw_serial": output,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--baud", type=int, default=921_600)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--gain", type=float, default=8.0)
    parser.add_argument("--durations", type=int, nargs="+", default=[1, 3, 5])
    parser.add_argument("--inject", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--boot-timeout", type=float, default=30.0)
    parser.add_argument("--round-timeout", type=float, default=90.0)
    args = parser.parse_args()

    clips = [
        load_pcm(DEFAULT_CLIPS / f"segment_{index:03d}.wav")
        for index in range(1, 11)
    ]
    monitor = SerialMonitor(args.port, args.baud)
    player = pyaudio.PyAudio()
    endpoint_volume = AudioUtilities.GetSpeakers().EndpointVolume
    original_volume = endpoint_volume.GetMasterVolumeLevelScalar()
    endpoint_volume.SetMasterVolumeLevelScalar(1.0, None)
    results = []
    try:
        boot = monitor.read_until(("[NET] v2 connected",), args.boot_timeout)
        if "[NET] v2 connected" not in boot:
            raise RuntimeError("device did not establish v2 connection")
        for round_index in range(args.rounds):
            duration = args.durations[round_index % len(args.durations)]
            pcm = make_duration(clips[round_index % len(clips)], duration)
            if args.inject:
                monitor.serial.write(f"TEST_PCM {len(pcm)}\n".encode("ascii"))
                monitor.serial.flush()
                # Pace USB CDC in protocol-sized units. A single 100 KB host
                # write can overrun the ESP32 Arduino RX ring even though the
                # OS reports the bytes accepted by its own USB buffer.
                for offset in range(0, len(pcm), 640):
                    frame_started = time.monotonic()
                    frame = pcm[offset:offset + 640]
                    for piece_offset in range(0, len(frame), 32):
                        monitor.serial.write(frame[piece_offset:piece_offset + 32])
                        monitor.serial.flush()
                        time.sleep(0.001)
                    remaining = 0.02 - (time.monotonic() - frame_started)
                    if remaining > 0:
                        time.sleep(remaining)
                pressed = ""
            else:
                monitor.write("TEST_PRESS")
                pressed = monitor.read_until(("ACK TEST_PRESS",), 3.0)
                if "ACK TEST_PRESS" not in pressed:
                    raise RuntimeError("device did not acknowledge TEST_PRESS")
                time.sleep(0.15)
                play_pcm(player, pcm, args.gain)
                time.sleep(0.08)
                monitor.write("TEST_RELEASE")
            finished = monitor.read_until(
                ("playback finished", "Guru Meditation"),
                args.round_timeout,
            )
            output = pressed + finished
            result = parse_round(round_index + 1, duration, output)
            results.append(result)
            print("\nROUND_RESULT " + json.dumps(result, ensure_ascii=False))
            if not result["completed"] or result["crashed"]:
                break
            time.sleep(0.25)
    finally:
        endpoint_volume.SetMasterVolumeLevelScalar(original_volume, None)
        player.terminate()
        monitor.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"rounds": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if len(results) != args.rounds or not all(
        row["completed"]
        and not row["crashed"]
        and row["underruns"] == 0
        and row["overflow_warnings"] == 0
        and row["sequence_errors"] == 0
        and row["stale_frames"] == 0
        and row["injection_timeouts"] == 0
        for row in results
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
