"""Diagnostics preserve original PCM and are opt-in, outside C:, and nonfatal."""
import json
import wave

from common.audio_capture import save_pcm, save_metadata


def test_disabled_capture_does_not_write(monkeypatch, tmp_path):
    monkeypatch.delenv('TOPPET_AUDIO_CAPTURE_DIR', raising=False)
    save_pcm('microphone', 'test', bytes(640))
    assert not list(tmp_path.iterdir())


def test_exact_samples_and_safe_filename(monkeypatch, tmp_path):
    monkeypatch.setenv('TOPPET_AUDIO_CAPTURE_DIR', str(tmp_path))
    pcm = b'\x01\x00\xff\x7f' * 160
    save_pcm('microphone', '../../unsafe', pcm)
    save_metadata('microphone', '../../unsafe', transcript='你好')
    wav = next(tmp_path.glob('*.wav'))
    with wave.open(str(wav), 'rb') as handle:
        assert handle.getframerate() == 16000
        assert handle.readframes(handle.getnframes()) == pcm
    assert json.loads(wav.with_suffix('.json').read_text(encoding='utf-8'))['transcript'] == '你好'


def test_invalid_destination_is_nonfatal(monkeypatch, tmp_path):
    monkeypatch.setenv('TOPPET_AUDIO_CAPTURE_DIR', 'relative/path')
    save_pcm('microphone', 'test', bytes(640))
    assert not list(tmp_path.iterdir())
