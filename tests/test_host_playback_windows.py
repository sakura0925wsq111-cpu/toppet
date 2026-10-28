from __future__ import annotations

import asyncio
import json
import random
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.sentence_chunker import SentenceChunker
from host_service.pipeline import HostPipeline, PipelineCancelled
from host_service.playback_windows import PlaybackWindowWriter, DEFAULT_WINDOW_BYTES
from audio.tts.voxcpm_engine import AudioChunk


TEXT = "今天的事情可以慢一点处理，不用一口气把所有问题都解决。先选一个最容易开始的小步骤，做完以后再看看接下来想做什么。如果暂时还没想好，也可以先坐一会儿，把刚才没说完的话慢慢讲清楚。"


def chunks_for(text, sizes):
    chunker = SentenceChunker(min_chars=4, max_chars=14, comma_chars=8)
    parts = []
    position = 0
    for size in sizes:
        parts.extend(chunker.feed(text[position:position + size]))
        position += size
    parts.extend(chunker.feed(text[position:]))
    return parts + chunker.flush()


@pytest.mark.parametrize("width", [1, 2, 3, 7, 14, 88, 10000])
def test_boundaries_are_lossless_bounded_and_independent_of_delta_width(width):
    reference = chunks_for(TEXT, [1] * len(TEXT))
    actual = chunks_for(TEXT, [width] * (len(TEXT) // width))
    assert actual == reference
    assert "".join(actual) == TEXT
    assert all(0 < len(part) <= 14 for part in actual)


def test_random_partitions_and_whitespace_do_not_change_boundaries_or_drop_text():
    rng = random.Random(42)
    for text in (TEXT, "嗯。  你好呀！\nNext; 好吗？ " * 10, "无标点" * 100):
        expected = chunks_for(text, [1] * len(text))
        for _ in range(10):
            sizes = []
            remaining = len(text)
            while remaining:
                size = min(remaining, rng.randint(1, 25))
                sizes.append(size)
                remaining -= size
            actual = chunks_for(text, sizes)
            assert actual == expected
            assert "".join(actual) == text
            assert all(len(part) <= 14 for part in actual)


@pytest.fixture(autouse=True)
def compact_ui(monkeypatch):
    monkeypatch.setattr("host_service.playback_windows.build_response_ui_event",
                        lambda text, emotion, sequence: {"text": text, "emotion": emotion, "sequence": sequence})


def make_writer(*, window=DEFAULT_WINDOW_BYTES, cancel=None):
    wire, frames = [], []
    async def event(kind, **data):
        wire.append((kind, data))
    async def audio(frame):
        frames.append(frame)
        wire.append(("pcm", frame))
    def check():
        if cancel is not None and cancel.is_set():
            raise PipelineCancelled("cancelled")
    writer = PlaybackWindowWriter(window_bytes=window, emit=event, send_audio=audio,
                                  check_cancel=check, on_first_audio=lambda seq: None)
    return writer, wire, frames


def validate_wire_ranges(wire):
    announced = 0
    written = 0
    expected_sequence = 0
    manifests = []
    for kind, data in wire:
        if kind == "tts_sentence_ready":
            assert written == announced
            assert data["stream_offset"] == written
            assert data["sequence"] == expected_sequence
            assert data["unit"] == "pcm_window"
            announced += data["bytes"]
            manifests.append(data["bytes"])
        elif kind == "pcm":
            assert data.sequence == expected_sequence
            expected_sequence += 1
            written += len(data.pcm)
            assert written <= announced
    assert written == announced
    return manifests


def test_audio_windows_cross_text_boundaries_with_exact_lengths_and_no_pcm_change():
    writer, wire, frames = make_writer()
    first = b"\x01\x00" * 15000
    second = b"\x02\x00" * 5000
    async def run():
        writer.begin_sentence("第一句。", "NEUTRAL")
        await writer.write(first)
        writer.end_sentence("第一句。")
        writer.begin_sentence("第二句。", "SAD")
        await writer.write(second)
        writer.end_sentence("第二句。")
        await writer.finish()
    asyncio.run(run())
    assert b"".join(f.pcm for f in frames) == first + second
    assert validate_wire_ranges(wire) == [25600, 14400]
    ui = [data for kind, data in wire if kind == "response_ui"]
    second_packet = next(f.sequence for f in frames if f.pcm.startswith(b"\x02\x00"))
    assert [data["sequence"] for data in ui] == [0, second_packet]
    assert sum(len(f.pcm) for f in frames if f.sequence < second_packet) == len(first)
    assert all(len(f.pcm) <= 4096 for f in frames)
    assert [data["emotion"] for data in ui] == ["NEUTRAL", "SAD"]
    # Sentence metadata is emitted before its real first PCM packet, including
    # when its start lies inside a playback window.
    for data in ui:
        ui_index = wire.index(("response_ui", data))
        frame_index = next(i for i, (kind, f) in enumerate(wire) if kind == "pcm" and f.sequence == data["sequence"])
        assert ui_index < frame_index


def test_window_forwards_before_the_rest_of_a_long_sentence_is_generated():
    writer, wire, frames = make_writer()
    async def run():
        writer.begin_sentence("未生成完的长句。", "NEUTRAL")
        await writer.write(bytes(25600))
        assert sum(len(f.pcm) for f in frames) == 25600
        await writer.write(bytes(1000))
        writer.end_sentence("未生成完的长句。")
        await writer.finish()
    asyncio.run(run())
    assert validate_wire_ranges(wire) == [25600, 1000]


def test_initial_headroom_is_real_pcm_and_later_units_keep_the_smaller_size():
    wire, frames = [], []
    async def emit(kind, **data):
        wire.append((kind, data))
    async def send(frame):
        frames.append(frame)
        wire.append(("pcm", frame))
    writer = PlaybackWindowWriter(window_bytes=25600, initial_window_bytes=40960,
                                  emit=emit, send_audio=send, check_cancel=lambda: None,
                                  on_first_audio=lambda seq: None)
    async def run():
        writer.begin_sentence("测试。", "NEUTRAL")
        await writer.write(bytes(40000))
        assert not frames
        await writer.write(bytes(10000))
        assert sum(len(f.pcm) for f in frames) == 40960
        await writer.write(bytes(20000))
        writer.end_sentence("测试。")
        await writer.finish()
    asyncio.run(run())
    assert validate_wire_ranges(wire) == [40960, 25600, 3440]
    assert sum(len(f.pcm) for f in frames) == 70000


def test_many_short_sentences_do_not_create_many_tiny_ready_descriptors():
    writer, wire, frames = make_writer()
    async def run():
        for _ in range(100):
            writer.begin_sentence("短句。", "NEUTRAL")
            await writer.write(bytes(640))
            writer.end_sentence("短句。")
        await writer.finish()
    asyncio.run(run())
    assert validate_wire_ranges(wire) == [25600, 25600, 12800]
    assert len([kind for kind, _ in wire if kind == "tts_sentence_start"]) == 100


@pytest.mark.parametrize("size", [640, 25600, 51200, 51202])
def test_short_tail_and_exact_window_multiple_finish_without_padding(size):
    writer, wire, frames = make_writer()
    async def run():
        writer.begin_sentence("测试。", "NEUTRAL")
        await writer.write(bytes(size))
        writer.end_sentence("测试。")
        await writer.finish()
    asyncio.run(run())
    validate_wire_ranges(wire)
    assert sum(len(f.pcm) for f in frames) == size
    assert wire[-1][0] == "tts_sentence_end"
    assert not writer.items and writer.buffered_bytes == 0


def test_cancelled_window_does_not_emit_a_late_tail():
    cancel = threading.Event()
    writer, wire, frames = make_writer(cancel=cancel)
    async def run():
        writer.begin_sentence("测试。", "NEUTRAL")
        await writer.write(bytes(640))
        cancel.set()
        with pytest.raises(PipelineCancelled):
            await writer.finish()
    asyncio.run(run())
    assert not wire and not frames


@pytest.mark.parametrize("value", [1, 5120, 16000, 25000, True, -25600, 25600.0])
def test_window_configuration_cannot_silently_shrink_into_an_unsafe_ready_queue(value):
    with pytest.raises(ValueError):
        HostPipeline(None, None, None, playback_window_bytes=value)


def test_existing_receiver_ready_queue_and_ring_backpressure_accept_a_long_burst():
    writer, wire, _ = make_writer()
    async def generate():
        writer.begin_sentence("长句。", "NEUTRAL")
        # A fast fake producer is deliberate; model receiver backpressure next.
        for _ in range(500):
            await writer.write(bytes(4096))
        writer.end_sentence("长句。")
        await writer.finish()
    asyncio.run(generate())
    capacity = 256 * 1024
    available = written = consumed = 0
    queue = deque()
    maximum = 0
    started = False
    def consume():
        nonlocal available, consumed, started
        while queue and queue[0] <= consumed:
            queue.popleft()
        if queue and available >= queue[0] - consumed:
            started = True
        assert started, "receiver could not start; incompatible range metadata"
        count = min(640, available)
        available -= count
        consumed += count
        while queue and queue[0] <= consumed:
            queue.popleft()
    for kind, data in wire:
        if kind == "tts_sentence_ready":
            queue.append(written + data["bytes"])
            maximum = max(maximum, len(queue))
            assert len(queue) <= 16
        elif kind == "pcm":
            remaining = len(data.pcm)
            while remaining:
                if available == capacity:
                    consume()
                count = min(remaining, capacity - available)
                available += count
                written += count
                remaining -= count
    while available:
        consume()
    assert consumed == 500 * 4096
    assert not queue and maximum <= 16


def test_from_config_pins_existing_configured_three_steps_without_changing_voice():
    profile = {"tts": {"engine": "voxcpm", "voice": "test-original-voice", "inference_steps": 3,
                       "adaptive_inference_steps": [3, 4], "allow_voice_fallback": False}}
    brain = SimpleNamespace(character_manager=SimpleNamespace(get_profile=lambda _: profile))
    with patch("host_service.pipeline.PetBrain", return_value=brain):
        pipeline = HostPipeline.from_config()
    assert pipeline.tts_engine._adaptive_steps == (3,)
    assert pipeline.tts_engine.voice_id == "test-original-voice"
    assert pipeline.playback_window_bytes == 25600
    assert pipeline.initial_playback_window_bytes == 40960


def test_cancel_during_real_window_forwarding_closes_producer_and_sends_no_late_pcm():
    class ASR:
        def transcribe_pcm(self, pcm, sample_rate=16000):
            return "固定测试"
    class Brain:
        def process_stream(self, text, on_emotion, on_text_delta, cancel_event):
            on_emotion("NEUTRAL")
            on_text_delta("这是固定的测试句子。")
            return SimpleNamespace(content="这是固定的测试句子。", emotion="NEUTRAL")
    class TTS:
        generated = 0
        closed = False
        def synthesize_stream(self, text, cancel_event=None):
            try:
                for sequence in range(20):
                    self.generated += 1
                    yield AudioChunk(bytes(4096), 16000, 1, 16, sequence, sequence == 19)
            finally:
                self.closed = True
    tts = TTS()
    pipeline = HostPipeline(Brain(), ASR(), tts)
    sent, events = [], []
    async def emit(event):
        events.append(event)
    async def sink(chunk):
        sent.append(chunk)
        pipeline.cancel_session("cancel-window")
    with pytest.raises(PipelineCancelled):
        asyncio.run(pipeline.process_streamed_audio(bytes(640), session_id="cancel-window",
                    device_id="test", emit=emit, send_audio=sink))
    assert tts.generated == 10
    assert tts.closed and len(sent) == 1
    assert not any(e["type"] == "tts_stop" for e in events)


def test_existing_v2_websocket_consumer_can_read_windowed_headers_and_binary_pcm():
    from fastapi.testclient import TestClient
    from host_service.server import create_app
    class ASR:
        engine_name = "contract-asr"
        def transcribe_pcm(self, pcm, sample_rate=16000):
            return "测试输入"
    class Brain:
        def process_stream(self, text, on_emotion, on_text_delta, cancel_event):
            on_emotion("NEUTRAL")
            on_text_delta("这是完整的测试句子。")
            return SimpleNamespace(content="这是完整的测试句子。", emotion="NEUTRAL")
        def close(self, wait=False):
            pass
    class TTS:
        engine_name = "contract-tts"
        def synthesize_stream(self, text, cancel_event=None):
            for seq in range(40):
                yield AudioChunk(b"\x01\x00" * 2048, 16000, 1, 16, seq, seq == 39)
    pipeline = HostPipeline(Brain(), ASR(), TTS())
    with TestClient(create_app(pipeline)) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "hello", "protocol": "toppet-host/v2", "device_id": "contract",
                "audio": {"format": "pcm_s16le", "sample_rate": 16000, "channels": 1, "bits_per_sample": 16, "frame_ms": 20}})
            assert websocket.receive_json()["type"] == "hello_ack"
            websocket.send_json({"type": "listen_start", "session_id": "windows"})
            assert websocket.receive_json()["state"] == "LISTENING"
            websocket.send_json({"type": "mic_frame", "session_id": "windows", "sequence": 0, "bytes": 640})
            websocket.send_bytes(bytes(640))
            websocket.send_json({"type": "listen_stop", "session_id": "windows"})
            written = declared = sequence = units = 0
            while True:
                event = websocket.receive_json()
                assert event["session_id"] == "windows"
                if event["type"] == "tts_sentence_ready":
                    assert written == declared == event["stream_offset"]
                    assert event["sequence"] == sequence
                    assert event["bytes"] > 0
                    declared += event["bytes"]
                    units += 1
                elif event["type"] == "tts_audio":
                    pcm = websocket.receive_bytes()
                    assert len(pcm) == event["bytes"]
                    assert event["sequence"] == sequence
                    sequence += 1
                    written += len(pcm)
                    assert written <= declared
                elif event["type"] == "tts_stop":
                    break
            assert written == declared == 40 * 4096 + 4 * 640
            assert units > 1
