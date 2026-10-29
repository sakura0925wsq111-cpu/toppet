import threading

import numpy as np
import pytest

from host_service.qwen_streaming import decode_complete_utterance
from audio.tts.qwen_engine import QwenTTSEngine
from types import SimpleNamespace


def test_buffered_decode_retains_all_codes_in_one_acoustic_call():
    chunks = [np.array([i, i + 1]) for i in (0, 2, 4)]
    seen = []
    def decode(parts):
        seen.append(parts)
        return np.cumsum(np.concatenate(parts))
    result = list(decode_complete_utterance(iter((c, {}) for c in chunks), decode, 24000))
    assert len(seen) == 1 and len(seen[0]) == 3
    np.testing.assert_array_equal(result[0][0], np.cumsum(np.arange(6)))


def test_cancelled_generation_never_decodes_or_outputs_old_audio():
    cancelled, closed = threading.Event(), []
    def source():
        try:
            yield np.ones(8), {}
            cancelled.set()
            yield np.ones(8), {}
        finally:
            closed.append(True)
    result = list(decode_complete_utterance(source(), lambda _: pytest.fail('must not decode'), 24000, cancelled))
    assert result == [] and closed


def test_invalid_audio_is_rejected_before_transport():
    with pytest.raises(RuntimeError, match='Invalid'):
        list(decode_complete_utterance(iter([(np.ones(8), {})]), lambda _: np.array([np.nan]), 24000))


def test_buffered_mode_is_explicit_and_does_not_change_voice_or_text():
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs['json'])
        return SimpleNamespace(status_code=200)
    engine = QwenTTSEngine(session=SimpleNamespace(post=post), delivery_mode='buffered')
    engine._post('/tts/stream', '你好', stream=True)
    assert calls == [{'text':'你好', 'voice_id':'qwen-user-20261028', 'delivery_mode':'buffered'}]
    with pytest.raises(ValueError):
        QwenTTSEngine(delivery_mode='truncated-context')
