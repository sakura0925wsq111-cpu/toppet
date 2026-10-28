import asyncio
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from audio.asr.whisper_asr import FasterWhisperEngine
from audio.tts.voxcpm_engine import AudioChunk
from host_service.subtitle import paginate_subtitle_text, _wrap_all_lines, MAX_LINES, build_response_ui_event
from host_service.hardware_control import HardwareControl, DeviceLink, SpeakRequest


def test_long_reply_has_complete_readable_pages_instead_of_ellipsis():
    text = '我记得你之前提到的事情。我们可以慢慢聊，不着急一次把所有内容说完。DeepSeek 和 QQ 的聊天背景也会保留，每一页字幕都应该能读到。'
    pages = paginate_subtitle_text(text)
    assert len(pages) > 1
    assert ''.join(pages) == text
    draw = ImageDraw.Draw(Image.new('1', (1, 1)))
    for index, page in enumerate(pages):
        assert len(_wrap_all_lines(page, draw)) <= MAX_LINES
        assert '…' not in page
        assert build_response_ui_event(page, 'HAPPY', index)['data']


def test_paged_reply_preserves_one_tts_request_and_exact_pcm():
    async def run():
        text = '这是一段需要分页显示的完整回复，不能只显示开头和省略号。声音仍然只合成一次，字幕按音频位置切换。'
        pcm = np.arange(16000, dtype='<i2').tobytes()
        class TTS:
            calls = []
            def synthesize_stream(self, value, **kwargs):
                self.calls.append(value)
                yield AudioChunk(pcm[:16000], 16000, 1, 16, 0, False)
                yield AudioChunk(pcm[16000:], 16000, 1, 16, 1, True)
        tts = TTS()
        control = HardwareControl(tts, token='fixture')
        subtitles, chunks, events = [], [], []
        async def send_json(message):
            events.append(message)
            if message['type'] == 'tts_sentence_start':
                subtitles.append(message['text'])
            if message['type'] == 'tts_stop':
                control.event(link, {'type': 'latency', 'session_id': 'mic-1', 'node': 'playback_finished'})
        async def send_audio(session, chunk):
            assert session == 'mic-1'
            chunks.append(chunk.pcm)
        link = DeviceLink('pet', True, send_json, send_audio, lambda _: False, lambda _: None,
                          legacy_busy=True, legacy_session='mic-1', resume_input=lambda sid: sid == 'mic-1')
        control.attach(link)
        accepted = control.speak(SpeakRequest(device_id='pet', input_session_id='mic-1',
                                              text=text, request_id='caption-test'))
        await control.actions[accepted['action_id']].task
        assert tts.calls == [text]
        assert b''.join(chunks) == pcm
        assert ''.join(subtitles) == text
        assert len(subtitles) > 1
        assert control.get(accepted['action_id'])['playback_confirmed']
    asyncio.run(run())


def test_short_voice_reaches_decoder_with_context_without_vad_erasure():
    seen = {}
    class Model:
        def transcribe(self, source, **options):
            seen.update({'source': source, **options})
            return iter([SimpleNamespace(text='在吗')]), None
    engine = FasterWhisperEngine(model_size='base')
    engine._model = Model()
    original = np.arange(6400, dtype='<i2').tobytes()  # 400ms, not discarded.
    assert engine.transcribe_pcm(original) == '在吗'
    assert seen['source'].size == 6400 + 1280 + 5120
    assert np.any(seen['source'][1280:1280+6400])
    assert seen['vad_filter'] is False
    assert seen['max_new_tokens'] == 32
    assert seen['condition_on_previous_text'] is False


def test_long_voice_keeps_all_samples():
    seen = {}
    class Model:
        def transcribe(self, source, **kwargs):
            seen['samples'] = source.size
            return iter([SimpleNamespace(text='完整长句')]), None
    engine = FasterWhisperEngine(model_size='base'); engine._model = Model()
    original = bytes(128000)
    assert engine.transcribe_pcm(original) == '完整长句'
    assert seen['samples'] == 64000
