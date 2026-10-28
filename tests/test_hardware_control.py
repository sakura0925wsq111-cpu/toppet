"""Hardware output contracts; no LLM, GPU, personal memory or physical device."""
import asyncio
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from audio.tts.voxcpm_engine import AudioChunk
from host_service.hardware_control import DeviceLink, HardwareControl, SpeakRequest, install_control_routes
from host_service.pipeline import HostPipeline
from host_service.server import create_app


class FakeTTS:
    engine_name = 'fake-tts'
    audio_format = 'pcm_s16le'

    def __init__(self):
        self.calls = 0

    def synthesize_stream(self, text, cancel_event=None):
        self.calls += 1
        yield AudioChunk(bytes(32000), 16000, 1, 16, 0, True)


class NoBrain:
    def process_stream(self, *args, **kwargs):
        raise AssertionError('hardware speech must not invoke a brain')

    def close(self, **kwargs):
        pass


def test_full_websocket_speech_requires_device_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv('TOPPET_CONTROL_TOKEN_FILE', str(tmp_path / 'token'))
    tts = FakeTTS()
    pipeline = HostPipeline(NoBrain(), type('ASR', (), {'engine_name': 'fake'})(), tts, character_id='personal')
    app = create_app(pipeline)
    app.state.hardware._token = 'test-token'
    headers = {'Authorization': 'Bearer test-token'}
    with TestClient(app, client=('127.0.0.1', 12345)) as client:
        with client.websocket_connect('/ws') as ws:
            ws.send_json({'type': 'hello', 'protocol': 'toppet-host/v2', 'device_id': 'pet-1',
                          'capabilities': ['remote_control_v1'], 'audio': {
                              'format': 'pcm_s16le', 'sample_rate': 16000, 'channels': 1,
                              'bits_per_sample': 16, 'frame_ms': 20}})
            assert ws.receive_json()['type'] == 'hello_ack'
            body = {'device_id': 'pet-1', 'text': '硬件工具测试。', 'request_id': 'request-1'}
            response = client.post('/api/control/speak', headers=headers, json=body)
            assert response.status_code == 202
            action_id = response.json()['action_id']
            assert client.post('/api/control/speak', headers=headers, json=body).json()['action_id'] == action_id
            assert client.post('/api/control/speak', headers=headers,
                               json={**body, 'request_id': 'request-2'}).status_code == 409
            assert ws.receive_json() == {'type': 'control_start', 'session_id': action_id}
            ws.send_json({'type': 'control_ready', 'session_id': action_id, 'status': 'ready'})
            total = 0
            while True:
                event = ws.receive_json()
                assert event['session_id'] == action_id
                if event['type'] == 'tts_audio':
                    pcm = ws.receive_bytes()
                    assert len(pcm) == event['bytes']
                    total += len(pcm)
                if event['type'] == 'tts_stop':
                    break
            assert total == 32000
            current = client.get('/api/control/actions/' + action_id, headers=headers).json()
            assert not current['playback_confirmed']
            ws.send_json({'type': 'latency', 'session_id': 'stale-session', 'node': 'playback_finished'})
            assert not client.get('/api/control/actions/' + action_id, headers=headers).json()['playback_confirmed']
            ws.send_json({'type': 'latency', 'session_id': action_id, 'node': 'playback_finished'})
            for _ in range(50):
                current = client.get('/api/control/actions/' + action_id, headers=headers).json()
                if current['state'] == 'completed':
                    break
                time.sleep(.01)
            assert current['playback_confirmed']
            assert tts.calls == 1


def test_authentication_offline_and_lan_rejection():
    control = HardwareControl(FakeTTS(), token='test')
    app = FastAPI()
    install_control_routes(app, control)
    with TestClient(app, client=('127.0.0.1', 12345)) as client:
        assert client.get('/api/control/devices').status_code == 401
        assert client.get('/api/control/devices', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        headers = {'Authorization': 'Bearer test'}
        assert client.get('/api/control/devices', headers=headers).json()['devices'] == []
        response = client.post('/api/control/speak', headers=headers,
                               json={'device_id': 'offline', 'text': 'test', 'request_id': 'req'})
        assert response.status_code == 409 and response.json()['detail'] == 'DEVICE_OFFLINE'
    with TestClient(app, client=('192.168.1.10', 12345)) as client:
        assert client.get('/api/control/devices', headers=headers).status_code == 403


def test_old_firmware_is_rejected_before_synthesis():
    control = HardwareControl(FakeTTS(), token='test')
    link = DeviceLink('old', False, None, None, lambda _: True, lambda _: None)
    control.attach(link)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        control.speak(SpeakRequest(device_id='old', text='test', request_id='req'))
    assert error.value.status_code == 409
    assert 'FIRMWARE_UPGRADE_REQUIRED' in error.value.detail
    assert not control.actions


@pytest.mark.parametrize('scenario,expected', [('busy', 'failed'), ('disconnect', 'failed'),
                                             ('abort', 'cancelled'), ('stop', 'cancelled'),
                                             ('timeout', 'failed'), ('press', 'cancelled')])
def test_handshake_failures_and_local_interrupt_do_not_produce_audio(scenario, expected):
    async def run():
        tts = FakeTTS()
        control = HardwareControl(tts, token='test', handshake_timeout=.03)
        frames = []
        async def send_json(message):
            if message['type'] != 'control_start':
                return
            session = message['session_id']
            if scenario == 'busy':
                control.event(link, {'type': 'control_ready', 'session_id': session, 'status': 'busy'})
            elif scenario == 'disconnect':
                control.detach(link)
            elif scenario == 'abort':
                control.event(link, {'type': 'abort', 'session_id': session})
            elif scenario == 'stop':
                await control.stop(session)
            elif scenario == 'press':
                control.event(link, {'type': 'listen_start', 'session_id': 'physical-turn'})
        async def send_audio(session, chunk):
            frames.append(chunk)
        link = DeviceLink('pet', True, send_json, send_audio, lambda _: True, lambda _: None)
        control.attach(link)
        snapshot = control.speak(SpeakRequest(device_id='pet', text='test', request_id='req'))
        action = control.actions[snapshot['action_id']]
        await action.task
        assert action.state == expected
        assert not frames and tts.calls == 0
        assert link.action_id is None
    asyncio.run(run())


def test_missing_playback_ack_is_not_success():
    async def run():
        control = HardwareControl(FakeTTS(), token='test', playback_timeout=.03)
        async def send_json(message):
            if message['type'] == 'control_start':
                control.event(link, {'type': 'control_ready', 'session_id': message['session_id'], 'status': 'ready'})
        async def send_audio(session, chunk):
            pass
        link = DeviceLink('pet', True, send_json, send_audio, lambda _: True, lambda _: None)
        control.attach(link)
        snapshot = control.speak(SpeakRequest(device_id='pet', text='test', request_id='req'))
        await control.actions[snapshot['action_id']].task
        assert control.get(snapshot['action_id'])['state'] == 'failed'
        assert not control.get(snapshot['action_id'])['playback_confirmed']
    asyncio.run(run())


def test_stop_during_stream_drops_all_remaining_pcm():
    async def run():
        control = HardwareControl(FakeTTS(), token='test')
        frames = []
        async def send_json(message):
            if message['type'] == 'control_start':
                # Premature playback reports cannot confirm an action.
                control.event(link, {'type': 'latency', 'node': 'playback_finished',
                                     'session_id': message['session_id']})
                assert control.actions[message['session_id']].state != 'completed'
                control.event(link, {'type': 'control_ready', 'session_id': message['session_id'], 'status': 'ready'})
        async def send_audio(session, chunk):
            frames.append(chunk)
            await control.stop(session)
        link = DeviceLink('pet', True, send_json, send_audio, lambda _: True, lambda _: None)
        control.attach(link)
        snapshot = control.speak(SpeakRequest(device_id='pet', text='test', request_id='req'))
        action = control.actions[snapshot['action_id']]
        await action.task
        assert action.state == 'cancelled'
        assert len(frames) == 1
        assert not action.output_complete
    asyncio.run(run())
