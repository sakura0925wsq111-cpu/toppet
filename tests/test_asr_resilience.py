"""Quiet speech preservation and real worker-process failure containment."""
import sys

import numpy as np
import pytest

from audio.asr.whisper_asr import prepare_hardware_audio
from audio.asr.isolated_whisper import IsolatedWhisperEngine


def test_startup_impulses_do_not_hide_quiet_speech_or_modify_original():
    time = np.arange(24000) / 16000
    source = (.002 * np.sin(2 * np.pi * 240 * time)).astype(np.float32)
    source[1360:1367] = 1
    source[2080:2087] = -1
    original = source.copy()
    processed = prepare_hardware_audio(source)
    np.testing.assert_array_equal(source, original)
    assert np.max(np.abs(processed[4000:])) > .1
    assert np.sqrt(np.mean(processed[4000:] ** 2)) > np.sqrt(np.mean(source[4000:] ** 2)) * 20
    assert np.max(np.abs(processed)) <= .98
    assert len(processed) == len(source)


def test_silence_and_constant_offset_do_not_become_speech():
    assert not np.any(prepare_hardware_audio(np.zeros(16000)))
    assert not np.any(prepare_hardware_audio(np.full(16000, .0001)))


def test_early_syllable_is_preserved_instead_of_cutting_first_200ms():
    time = np.arange(16000) / 16000
    source = (.05 * np.sin(2 * np.pi * 400 * time)).astype(np.float32)
    output = prepare_hardware_audio(source)
    assert np.corrcoef(source[:2400], output[:2400])[0, 1] > .99


def test_worker_crash_fails_only_that_request_and_next_request_recovers(tmp_path):
    marker = str(tmp_path / 'crashed-once')
    code = f"""
import sys,json,os
from pathlib import Path
print(json.dumps({{'ready':True}}),flush=True)
for line in sys.stdin:
    marker=Path({marker!r})
    if not marker.exists():
        marker.touch()
        os._exit(7)
    print(json.dumps({{'text':'你好'}}),flush=True)
"""
    engine = IsolatedWhisperEngine(command=[sys.executable, '-u', '-c', code], startup_timeout=5)
    try:
        with pytest.raises(RuntimeError, match='exited'):
            engine.transcribe_pcm(bytes(640))
        assert engine.transcribe_pcm(bytes(640)) == '你好'
    finally:
        engine.close()


def test_worker_timeout_is_bounded_and_owned_process_is_reaped():
    code = "import json,time; print(json.dumps({'ready':True}),flush=True); time.sleep(30)"
    engine = IsolatedWhisperEngine(command=[sys.executable, '-u', '-c', code], timeout=.05, startup_timeout=5)
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            engine.transcribe_pcm(bytes(640))
        assert engine._process is None
    finally:
        engine.close()
