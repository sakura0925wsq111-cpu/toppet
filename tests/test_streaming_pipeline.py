from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from audio.tts.base_tts import TTSEngine  # noqa: E402
from audio.tts.tts_manager import TTSManager  # noqa: E402
from core.client import (  # noqa: E402
    AIClient,
    AIReply,
    StreamCancelled,
    StreamCancelToken,
)
from core.config import PetConfig  # noqa: E402
from core.pet_brain import PetBrain  # noqa: E402
from core.sentence_chunker import SentenceChunker  # noqa: E402
from ui.ai_worker import AIWorker  # noqa: E402


class _BlockingMemory:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, str]] = []
        self.file_path = "fake-memory.json"

    def get_prompt_text(self) -> str:
        return ""

    def extract_from_conversation(self, user_text, reply, _client) -> None:
        self.calls.append((user_text, reply))
        self.started.set()
        self.release.wait(2)


class BackgroundMemoryTests(unittest.TestCase):
    def _brain_with_reply(self) -> PetBrain:
        brain = PetBrain(PetConfig(api_key="test-key"), character="personal")
        brain.client.process = lambda **_kwargs: AIReply("HAPPY", "马上回答。")
        return brain

    def test_main_reply_does_not_wait_for_memory_extraction(self):
        brain = self._brain_with_reply()
        memory = _BlockingMemory()
        brain._memories["personal"] = memory
        try:
            started = time.monotonic()
            reply = brain.process("你好")
            elapsed = time.monotonic() - started
            self.assertEqual(reply.content, "马上回答。")
            self.assertLess(elapsed, 0.3)
            self.assertTrue(memory.started.wait(1))
        finally:
            memory.release.set()
            brain.close(wait=True)

    def test_character_memory_is_captured_when_task_is_submitted(self):
        brain = self._brain_with_reply()
        personal = _BlockingMemory()
        shinchan = _BlockingMemory()
        brain._memories["personal"] = personal
        brain._memories["shinchan"] = shinchan
        try:
            brain.process("记住这一轮")
            self.assertTrue(personal.started.wait(1))
            brain.current_character = "shinchan"
            personal.release.set()
            deadline = time.monotonic() + 1
            while not personal.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(personal.calls), 1)
            self.assertEqual(shinchan.calls, [])
        finally:
            personal.release.set()
            shinchan.release.set()
            brain.close(wait=True)


class SentenceChunkerTests(unittest.TestCase):
    def test_arbitrary_deltas_are_lossless_and_short_clauses_merge(self):
        text = "嗯。今天过得怎么样？我一直都在这里陪着你！"
        chunker = SentenceChunker(min_chars=6, max_chars=20)
        output: list[str] = []
        for delta in ["嗯", "。今", "天过得", "怎么样", "？我一直", "都在这里陪着你", "！"]:
            output.extend(chunker.feed(delta))
        output.extend(chunker.flush())
        self.assertEqual("".join(output), text)
        self.assertTrue(output[0].startswith("嗯。今天"))

    def test_long_unpunctuated_text_is_forced_to_split(self):
        text = "这是一个没有任何标点而且会持续很长很长时间的模型回复用来测试首句不会无限等待"
        chunker = SentenceChunker(min_chars=4, max_chars=16)
        output = chunker.feed(text) + chunker.flush()
        self.assertGreater(len(output), 1)
        self.assertEqual("".join(output), text)
        self.assertTrue(all(len(part) <= 16 for part in output[:-1]))

    def test_newline_and_english_punctuation_are_preserved(self):
        text = "Hello!\n今天好吗？Fine;继续聊。"
        chunker = SentenceChunker(min_chars=3, max_chars=30)
        output = chunker.feed(text) + chunker.flush()
        self.assertEqual("".join(output), text)


class _FakeCompletions:
    def __init__(self, stream_factory, fallback_text=None):
        self.stream_factory = stream_factory
        self.fallback_text = fallback_text
        self.stream_calls = 0
        self.fallback_calls = 0

    def create(self, **kwargs):
        if kwargs.get("stream"):
            self.stream_calls += 1
            return self.stream_factory()
        self.fallback_calls += 1
        message = SimpleNamespace(content=self.fallback_text)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _chunk(text: str):
    return {"choices": [{"delta": {"content": text}}]}


class StreamingClientTests(unittest.TestCase):
    def _client(self, completions: _FakeCompletions) -> AIClient:
        client = AIClient(api_key="test-key")
        client._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        return client

    def test_parser_handles_arbitrary_chunk_boundaries(self):
        completions = _FakeCompletions(
            lambda: iter([_chunk("HA"), _chunk("PPY\n你"), _chunk("好。")])
        )
        emotions: list[str] = []
        deltas: list[str] = []
        reply = self._client(completions).process_stream(
            "聊聊天",
            system_prompt="stream",
            fallback_system_prompt="json",
            on_emotion=emotions.append,
            on_text_delta=deltas.append,
        )
        self.assertEqual(reply.emotion, "HAPPY")
        self.assertEqual(reply.content, "你好。")
        self.assertEqual("".join(deltas), "你好。")
        self.assertEqual(emotions, ["HAPPY"])

    def test_failure_before_content_uses_non_stream_fallback(self):
        def failing_stream():
            raise RuntimeError("network down")
            yield  # pragma: no cover

        completions = _FakeCompletions(
            failing_stream,
            fallback_text='{"emotion":"SAD","content":"回退成功。"}',
        )
        deltas: list[str] = []
        reply = self._client(completions).process_stream(
            "聊聊天",
            system_prompt="stream",
            fallback_system_prompt="json",
            on_text_delta=deltas.append,
        )
        self.assertEqual(reply.content, "回退成功。")
        self.assertEqual(completions.stream_calls, 1)
        self.assertEqual(completions.fallback_calls, 1)
        self.assertEqual(deltas, ["回退成功。"])

    def test_failure_after_content_keeps_partial_without_retry(self):
        def interrupted_stream():
            yield _chunk("HAPPY\n已经收到。")
            raise RuntimeError("connection reset")

        completions = _FakeCompletions(
            interrupted_stream,
            fallback_text='{"emotion":"SAD","content":"不应出现。"}',
        )
        reply = self._client(completions).process_stream(
            "聊聊天",
            system_prompt="stream",
            fallback_system_prompt="json",
        )
        self.assertEqual(reply.content, "已经收到。")
        self.assertEqual(completions.fallback_calls, 0)

    def test_cancel_closes_blocked_stream_and_suppresses_final_reply(self):
        class ClosableStream:
            def __init__(self):
                self.closed = threading.Event()
                self.waiting = threading.Event()
                self.first = True

            def __iter__(self):
                return self

            def __next__(self):
                if self.first:
                    self.first = False
                    return _chunk("HAPPY\n开头。")
                self.waiting.set()
                self.closed.wait(2)
                raise StopIteration

            def close(self):
                self.closed.set()

        stream = ClosableStream()
        completions = _FakeCompletions(lambda: stream)
        client = self._client(completions)
        token = StreamCancelToken()
        outcome: list[BaseException | str] = []

        def run():
            try:
                client.process_stream(
                    "聊聊天",
                    system_prompt="stream",
                    fallback_system_prompt="json",
                    cancel_event=token,
                )
                outcome.append("unexpected reply")
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(stream.waiting.wait(1))
        token.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], StreamCancelled)


class AIWorkerStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_first_sentence_is_emitted_before_full_reply_completes(self):
        continue_generation = threading.Event()

        class FakeBrain:
            def process_stream(
                self, _text, on_emotion, on_text_delta, cancel_event
            ):
                on_emotion("HAPPY")
                on_text_delta("第一句已经好了。")
                continue_generation.wait(2)
                if cancel_event.is_set():
                    raise StreamCancelled()
                on_text_delta("第二句随后完成。")
                return AIReply("HAPPY", "第一句已经好了。第二句随后完成。")

        worker = AIWorker(FakeBrain(), "你好")
        sentences: list[str] = []
        completed = threading.Event()
        worker.sentence_ready.connect(sentences.append)
        worker.response_ready.connect(lambda _reply: completed.set())
        try:
            worker.start()
            deadline = time.monotonic() + 1
            while not sentences and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.assertEqual(sentences, ["第一句已经好了。"])
            self.assertFalse(completed.is_set())
            continue_generation.set()
            deadline = time.monotonic() + 1
            while not completed.is_set() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.assertTrue(completed.is_set())
            self.assertEqual(
                sentences,
                ["第一句已经好了。", "第二句随后完成。"],
            )
        finally:
            continue_generation.set()
            worker.cancel_request()
            worker.wait(1000)


class _FakeEngine(TTSEngine):
    def __init__(self, on_synthesize=None):
        self.calls: list[str] = []
        self.on_synthesize = on_synthesize

    def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        if self.on_synthesize is not None:
            self.on_synthesize(text)
        return text.encode("utf-8")

    @property
    def engine_name(self) -> str:
        return "fake"


class StreamingTTSManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, event: threading.Event, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while not event.is_set() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        return event.is_set()

    def test_segments_are_synthesized_and_played_in_order(self):
        played: list[str] = []
        started_signals: list[bool] = []
        finished_signals: list[bool] = []
        finished = threading.Event()
        manager = TTSManager(
            engine=_FakeEngine(),
            audio_player=lambda data, _fmt, _stop: played.append(data.decode()),
        )
        manager.playback_started.connect(lambda: started_signals.append(True))
        manager.playback_finished.connect(
            lambda: (finished_signals.append(True), finished.set())
        )
        try:
            manager.begin_stream("one")
            manager.enqueue_text("one", "第一句。")
            manager.enqueue_text("one", "第二句。")
            manager.end_stream("one")
            self.assertTrue(self._wait(finished))
            self.assertEqual(played, ["第一句。", "第二句。"])
            self.assertEqual(started_signals, [True])
            self.assertEqual(finished_signals, [True])
        finally:
            manager.shutdown()

    def test_second_segment_synthesizes_while_first_is_playing(self):
        first_playing = threading.Event()
        release_first = threading.Event()
        second_synthesized = threading.Event()
        finished = threading.Event()

        def on_synthesize(text: str) -> None:
            if text == "第二句。":
                second_synthesized.set()

        def player(data: bytes, _fmt: str, _stop: threading.Event) -> None:
            if data.decode() == "第一句。":
                first_playing.set()
                release_first.wait(2)

        manager = TTSManager(
            engine=_FakeEngine(on_synthesize), audio_player=player
        )
        manager.playback_finished.connect(finished.set)
        try:
            manager.begin_stream("overlap")
            manager.enqueue_text("overlap", "第一句。")
            manager.enqueue_text("overlap", "第二句。")
            manager.end_stream("overlap")
            self.assertTrue(first_playing.wait(1))
            self.assertTrue(second_synthesized.wait(1))
            self.assertFalse(finished.is_set())
            release_first.set()
            self.assertTrue(self._wait(finished))
        finally:
            release_first.set()
            manager.shutdown()

    def test_end_waits_for_all_synthesis_and_cancel_drops_stale_audio(self):
        old_started = threading.Event()
        release_old = threading.Event()
        played: list[str] = []
        finished = threading.Event()

        def on_synthesize(text: str) -> None:
            if text == "旧回复。":
                old_started.set()
                release_old.wait(2)

        manager = TTSManager(
            engine=_FakeEngine(on_synthesize),
            audio_player=lambda data, _fmt, _stop: played.append(data.decode()),
        )
        manager.playback_finished.connect(finished.set)
        try:
            manager.begin_stream("old")
            manager.enqueue_text("old", "旧回复。")
            self.assertTrue(old_started.wait(1))
            manager.begin_stream("new")
            manager.enqueue_text("new", "新回复。")
            manager.end_stream("new")
            self.assertFalse(finished.is_set())
            release_old.set()
            self.assertTrue(self._wait(finished))
            self.assertEqual(played, ["新回复。"])
        finally:
            release_old.set()
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
