import asyncio

import numpy as np
import pytest

from audio.tts.voxcpm_engine import AudioChunk
from host_service.hardware_control import HardwareControl, DeviceLink, SpeakRequest


def test_compensates_codec_boost_without_changing_audio_length(monkeypatch):
    monkeypatch.setenv('TOPPET_SPEAKER_GAIN_DB', '-12.5')
    source = np.full(16000, 30000, dtype='<i2').tobytes()
    class TTS:
        def synthesize_stream(self, *args, **kwargs):
            yield AudioChunk(source, 16000, 1, 16, 0, True)
    async def run():
        control = HardwareControl(TTS(), token='test')
        output = bytearray()
        async def event(message):
            if message['type'] == 'tts_stop':
                control.event(link, {'type':'latency', 'node':'playback_finished', 'session_id':'input'})
        async def audio(session, chunk):
            output.extend(chunk.pcm)
        link = DeviceLink('pet', True, event, audio, lambda _: False, lambda _: None,
                          legacy_busy=True, legacy_session='input', resume_input=lambda _: True)
        control.attach(link)
        accepted = control.speak(SpeakRequest(device_id='pet', input_session_id='input', text='你好',request_id='gain-test'))
        await control.actions[accepted['action_id']].task
        assert len(output) == len(source)
        samples = np.frombuffer(output, dtype='<i2')
        assert np.all(np.abs(samples) <= 7115)
        # +12.5 dB at the existing codec recovers the source with rounding error.
        assert abs(float(samples.mean()) * 10 ** (12.5 / 20) - 30000) < 3
        assert control.get(accepted['action_id'])['playback_confirmed']
    asyncio.run(run())


@pytest.mark.parametrize('value', ['nan', 'inf', '10', '-50'])
def test_invalid_gain_is_rejected(monkeypatch, value):
    monkeypatch.setenv('TOPPET_SPEAKER_GAIN_DB', value)
    with pytest.raises(ValueError):
        HardwareControl(None)
