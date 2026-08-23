from __future__ import annotations

import asyncio
import json
import threading
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from audio.tts.voxcpm_engine import (
    AudioChunk,
    IncrementalPCMResampler,
    VoxCPMTTSEngine,
)
from host_service.pipeline import HostPipeline, PipelineCancelled
from host_service.playback_policy import PlaybackRingModel
from host_service.server import create_app


class _PCMResponse:
    status_code = 200
    text = ""
    headers = {
        "X-Sample-Rate": "48000",
        "X-Channels": "1",
        "X-Bits-Per-Sample": "16",
    }

    def __init__(self, pieces: list[bytes]):
        self.pieces = pieces
        self.closed = False

    def iter_content(self, chunk_size=4096):
        del chunk_size
        yield from self.pieces

    def close(self):
        self.closed = True


class _PCMSession:
    def __init__(self, response: _PCMResponse):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class _MemoryASR:
    @property
    def engine_name(self):
        return "memory-asr"

    def transcribe_pcm(self, pcm, sample_rate=16_000):
        assert sample_rate == 16_000
        assert pcm
        return "请连续回答"


class _StreamingBrain:
    def __init__(self, tts_started: threading.Event):
        self.tts_started = tts_started
        self.first_sentence_overlapped = False

    def process_stream(
        self, text, on_emotion=None, on_text_delta=None, cancel_event=None
    ):
        del text
        on_emotion("HAPPY")
        on_text_delta("第一句已经好了。")
        self.first_sentence_overlapped = self.tts_started.wait(2)
        if cancel_event is not None and cancel_event.is_set():
            raise PipelineCancelled("cancelled")
        on_text_delta("第二句随后完成。")
        return SimpleNamespace(
            emotion="HAPPY",
            content="第一句已经好了。第二句随后完成。",
        )

    def close(self, wait=False):
        del wait


class _StreamingTTS:
    def __init__(self, started: threading.Event):
        self.started = started
        self.sentences: list[str] = []

    @property
    def engine_name(self):
        return "streaming-tts"

    @property
    def audio_format(self):
        return "pcm_s16le"

    def synthesize_stream(self, text, cancel_event=None):
        self.sentences.append(text)
        self.started.set()
        if cancel_event is None or not cancel_event.is_set():
            yield AudioChunk(bytes(640), 16_000, 1, 16, 0, True)


class LowLatencyAudioTests(unittest.TestCase):
    def test_incremental_resampling_matches_unsplit_stream(self):
        source = b"".join(
            int((index % 2000) - 1000).to_bytes(2, "little", signed=True)
            for index in range(4803)
        )
        whole = IncrementalPCMResampler(48_000)
        expected = whole.feed(source, final=True)

        split = IncrementalPCMResampler(48_000)
        offsets = (1, 17, 222, 509, 2049, 4097, len(source))
        actual = bytearray()
        start = 0
        for end in offsets:
            actual.extend(split.feed(source[start:end]))
            start = end
        actual.extend(split.feed(source[start:], final=True))
        self.assertEqual(bytes(actual), expected)

    def test_voxcpm_stream_has_continuous_pcm_and_final_marker(self):
        source = b"".join(
            int(index - 1000).to_bytes(2, "little", signed=True)
            for index in range(2000)
        )
        response = _PCMResponse([source[:1], source[1:333], source[333:]])
        session = _PCMSession(response)
        engine = VoxCPMTTSEngine(session=session, inference_steps=2)
        chunks = list(engine.synthesize_stream("你好世界"))

        expected_converter = IncrementalPCMResampler(48_000)
        expected = expected_converter.feed(source, final=True)
        self.assertEqual(b"".join(chunk.pcm for chunk in chunks), expected)
        self.assertEqual([chunk.sequence for chunk in chunks], list(range(len(chunks))))
        self.assertTrue(chunks[-1].is_final)
        self.assertTrue(response.closed)
        self.assertEqual(session.calls[0][0], "http://127.0.0.1:9890/tts/stream")
        self.assertTrue(session.calls[0][1]["stream"])

    def test_ring_buffer_prebuffer_underflow_overflow_and_stale_session(self):
        ring = PlaybackRingModel(capacity=4096, prebuffer=1280, low_water=640, high_water=3200)
        ring.start("new")
        self.assertFalse(ring.push("old", bytes(640)))
        ring.push("new", bytes(640))
        self.assertEqual(ring.read(640), b"")
        ring.push("new", bytes(640))
        self.assertEqual(len(ring.read(640)), 640)
        self.assertFalse(ring.low)
        self.assertEqual(len(ring.read(640)), 640)
        self.assertTrue(ring.low)
        self.assertEqual(ring.read(640), b"")
        self.assertEqual(ring.underruns, 1)
        ring.push("new", bytes(4096))
        with self.assertRaises(BufferError):
            ring.push("new", b"x")
        ring.abort()
        self.assertEqual(ring.buffer, b"")


class StreamingPipelineTests(unittest.TestCase):
    def _pipeline(self):
        started = threading.Event()
        brain = _StreamingBrain(started)
        tts = _StreamingTTS(started)
        return HostPipeline(brain, _MemoryASR(), tts), brain, tts

    def test_first_sentence_enters_tts_before_llm_finishes_and_order_is_stable(self):
        pipeline, brain, tts = self._pipeline()
        events = []
        audio = []

        async def run():
            await pipeline.process_streamed_audio(
                bytes(640),
                session_id="pipeline-1",
                device_id="test-device",
                emit=lambda event: _append(events, event),
                send_audio=lambda chunk: _append(audio, chunk),
            )

        asyncio.run(run())
        self.assertTrue(brain.first_sentence_overlapped)
        self.assertEqual(tts.sentences, ["第一句已经好了。", "第二句随后完成。"])
        self.assertEqual(
            [chunk.sequence for chunk in audio], list(range(len(audio)))
        )
        self.assertEqual(sum(chunk.pcm == bytes(640) for chunk in audio), 26)
        event_types = [event["type"] for event in events]
        self.assertLess(event_types.index("stt_final"), event_types.index("tts_start"))
        self.assertEqual(
            [event["text"] for event in events if event["type"] == "tts_sentence_start"],
            tts.sentences,
        )
        subtitles = [event for event in events if event["type"] == "subtitle"]
        self.assertEqual(len(subtitles), 2)

    def test_cancelled_session_stops_before_late_second_sentence(self):
        pipeline, _brain, _tts = self._pipeline()
        first_audio = asyncio.Event()

        async def send_audio(_chunk):
            first_audio.set()
            pipeline.cancel_session("cancel-me")

        async def run():
            with self.assertRaises(PipelineCancelled):
                await pipeline.process_streamed_audio(
                    bytes(640),
                    session_id="cancel-me",
                    device_id="test-device",
                    send_audio=send_audio,
                )

        asyncio.run(run())


class V2WebSocketTests(unittest.TestCase):
    def setUp(self):
        self.pipeline, self.brain, self.tts = StreamingPipelineTests()._pipeline()
        self.client = TestClient(create_app(self.pipeline))

    def _hello(self, websocket):
        websocket.send_json({
            "type": "hello",
            "protocol": "toppet-host/v2",
            "device_id": "test-device",
            "audio": {
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "bits_per_sample": 16,
                "frame_ms": 20,
            },
        })
        self.assertEqual(websocket.receive_json()["protocol"], "toppet-host/v2")

    def test_v2_event_order_binary_audio_and_reconnect(self):
        with self.client as client:
            with client.websocket_connect("/ws") as websocket:
                self._hello(websocket)
                websocket.send_json({"type": "listen_start", "session_id": "ws-1"})
                self.assertEqual(websocket.receive_json()["state"], "LISTENING")
                websocket.send_json({
                    "type": "mic_frame",
                    "session_id": "ws-1",
                    "sequence": 0,
                    "bytes": 640,
                })
                websocket.send_bytes(bytes(640))
                websocket.send_json({"type": "listen_stop", "session_id": "ws-1"})
                events, binary_count = _receive_until_stop(websocket)
                self.assertGreater(binary_count, 0)
                self.assertIn("tts_audio", events)
                self.assertLess(events.index("stt_final"), events.index("tts_audio"))

            with client.websocket_connect("/ws") as websocket:
                self._hello(websocket)

    def test_v2_detects_missing_sequence_and_abort_is_session_scoped(self):
        with self.client as client:
            with client.websocket_connect("/ws") as websocket:
                self._hello(websocket)
                websocket.send_json({"type": "listen_start", "session_id": "gap"})
                websocket.receive_json()
                websocket.send_json({
                    "type": "mic_frame",
                    "session_id": "gap",
                    "sequence": 1,
                    "bytes": 640,
                })
                mismatch = websocket.receive_json()
                self.assertEqual(mismatch["code"], "PCM_SEQUENCE_MISMATCH")
                websocket.send_bytes(bytes(640))
                websocket.send_json({"type": "abort", "session_id": "gap"})
                cancelled = websocket.receive_json()
                self.assertEqual(cancelled, {"type": "cancelled", "session_id": "gap"})


async def _append(target, value):
    target.append(value)


def _receive_until_stop(websocket):
    event_types = []
    binary_count = 0
    while "tts_stop" not in event_types:
        message = websocket.receive()
        if message.get("bytes") is not None:
            binary_count += 1
            continue
        event = json.loads(message["text"])
        event_types.append(event["type"])
    return event_types, binary_count


if __name__ == "__main__":
    unittest.main()
