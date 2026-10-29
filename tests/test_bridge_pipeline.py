"""Contracts for microphone routing: no local LLM, no duplicate TTS path."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from host_service.bridge_pipeline import BridgePipeline
from host_service.hardware_control import HardwareControl, DeviceLink, SpeakRequest
from host_service.pipeline import PipelineCancelled
from audio.tts.voxcpm_engine import AudioChunk


class NoLocalTTS:
    engine_name = 'test'
    def synthesize(self, *args, **kwargs):
        raise AssertionError('input path cannot synthesize an independent reply')


def pipeline_fixture(tmp_path, handler):
    token = tmp_path / 'gateway-token'
    token.write_text('fixture-secret', encoding='utf-8')
    config = {'enabled': True, 'userQQ': '12345', 'deviceId': 'pet-1', 'port': 8781,
              'gatewayTokenFile': str(token)}
    factory = lambda **kwargs: httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handler))
    pipeline = BridgePipeline(SimpleNamespace(engine_name='fake-asr'), NoLocalTTS(), config,
                              character_manager=SimpleNamespace(), client_factory=factory)
    pipeline._transcribe_pcm = lambda *args: '你记得我准备的课程吗'
    return pipeline


def test_asr_text_enters_gateway_without_brain_or_local_tts(tmp_path):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.headers['authorization'] == 'Bearer fixture-secret'
        if request.method == 'POST':
            body = json.loads(request.content)
            assert body['transcript'] == '你记得我准备的课程吗'
            assert body['session_id'] == 'mic-1'
            assert 'user_id' not in body
            assert pipeline.input_ready('mic-1')
            return httpx.Response(202, json={'turn_id': 'hw-1'})
        return httpx.Response(200, json={'state': 'completed', 'playback_confirmed': True, 'reply': '交通课程。'})
    pipeline = pipeline_fixture(tmp_path, handler)
    assert pipeline.brain is None
    with patch('core.pet_brain.PetBrain.process_stream', side_effect=AssertionError('PetBrain must not run')):
        result = asyncio.run(pipeline.process_streamed_audio(bytes(640), session_id='mic-1', device_id='pet-1'))
    assert result.reply == '交通课程。'
    assert calls == [('POST', '/v1/hardware/turn'), ('GET', '/v1/hardware/turn/hw-1')]
    assert not pipeline.input_ready('mic-1')


def test_local_abort_cancels_only_corresponding_gateway_turn(tmp_path):
    calls = []
    def handler(request):
        calls.append((request.url.path, json.loads(request.content) if request.content else {}))
        if request.url.path == '/v1/hardware/turn':
            return httpx.Response(202, json={'turn_id': 'hw-old'})
        if request.method == 'GET':
            pipeline.cancel_session('old-mic')
            return httpx.Response(200, json={'state': 'model'})
        return httpx.Response(200, json={'state': 'cancelled'})
    pipeline = pipeline_fixture(tmp_path, handler)
    with pytest.raises(PipelineCancelled):
        asyncio.run(pipeline.process_streamed_audio(bytes(640), session_id='old-mic', device_id='pet-1'))
    assert calls[-1] == ('/v1/hardware/cancel', {'device_id': 'pet-1', 'session_id': 'old-mic'})


def test_gateway_unavailable_never_falls_back_to_petbrain(tmp_path):
    def handler(request):
        raise httpx.ConnectError('offline')
    pipeline = pipeline_fixture(tmp_path, handler)
    with patch('core.pet_brain.PetBrain.process_stream', side_effect=AssertionError('no fallback')):
        with pytest.raises(httpx.ConnectError):
            asyncio.run(pipeline.process_streamed_audio(bytes(640), session_id='offline', device_id='pet-1'))


def test_factory_does_not_instantiate_petbrain_or_chat_client(tmp_path):
    token = tmp_path / 'token'; token.write_text('fixture', encoding='utf-8')
    config = tmp_path / 'channel.json'
    config.write_text(json.dumps({'enabled': True, 'userQQ': '12345', 'deviceId': 'pet-1',
                                 'port': 8781, 'gatewayTokenFile': str(token)}), encoding='utf-8')
    manager = SimpleNamespace(get_profile=lambda _: {'tts': {'engine': 'qwen', 'voice': 'test-voice'}})
    with patch('core.pet_brain.PetBrain.__init__', side_effect=AssertionError('must not instantiate PetBrain')), \
            patch('core.client.AIClient.__init__', side_effect=AssertionError('must not instantiate AIClient')), \
            patch('host_service.bridge_pipeline.CharacterManager', return_value=manager), \
            patch('host_service.bridge_pipeline.IsolatedWhisperEngine'):
        pipeline = BridgePipeline.from_bridge_config(config)
        assert pipeline.brain is None
        assert pipeline.tts_engine.voice_id == 'test-voice'


def test_reply_resumes_physical_wire_session_without_new_control_start():
    async def run():
        class TTS:
            def synthesize_stream(self, *args, **kwargs):
                yield AudioChunk(bytes(32000), 16000, 1, 16, 0, True)
        control = HardwareControl(TTS(), token='test')
        events = []
        async def send_json(message):
            events.append(message)
            assert message['session_id'] == 'physical-input'
            if message['type'] == 'tts_stop':
                control.event(link, {'type': 'latency', 'session_id': 'physical-input', 'node': 'playback_finished'})
        async def send_audio(session, chunk):
            assert session == 'physical-input'
        def no_activate(_):
            raise AssertionError('must not replace physical session with a new control session')
        link = DeviceLink('pet', True, send_json, send_audio, no_activate, lambda _: None,
                          legacy_busy=True, legacy_session='physical-input',
                          resume_input=lambda session: session == 'physical-input')
        control.attach(link)
        response = control.speak(SpeakRequest(device_id='pet', text='回复', request_id='bridge-turn',
                                               input_session_id='physical-input'))
        await control.actions[response['action_id']].task
        assert not any(event['type'] == 'control_start' for event in events)
        assert control.get(response['action_id'])['playback_confirmed']
        assert not link.legacy_busy
    asyncio.run(run())
