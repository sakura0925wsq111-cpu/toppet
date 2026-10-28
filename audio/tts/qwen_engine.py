"""Qwen sentence synthesis over HTTP; only the companion process imports torch."""
from __future__ import annotations

import os
import threading
from collections.abc import Iterator

import requests

from .base_tts import TTSEngine
from .voxcpm_engine import AudioChunk, IncrementalPCMResampler


def resolve_tts_config(profile: dict) -> dict:
    config = dict(profile.get('tts', {}))
    override = os.getenv('TOPPET_TTS_ENGINE', '').strip().lower()
    config['qwen_runtime'] = os.getenv('TOPPET_QWEN_RUNTIME', config.get('qwen_runtime', 'baseline'))
    if override and override not in {'qwen', 'voxcpm', 'edge'}:
        raise ValueError(f'Unknown TOPPET_TTS_ENGINE: {override}')
    if override and override != config.get('engine'):
        config['engine'] = override
        if override in {'qwen', 'voxcpm'}:
            config['api_url'] = 'http://127.0.0.1:' + ('9891' if override == 'qwen' else '9890')
            config['voice'] = 'qwen-user-20261028' if override == 'qwen' else 'laoshaozi-personal'
    return config


class QwenTTSEngine(TTSEngine):
    def __init__(self, api_url='http://127.0.0.1:9891', timeout=180.0,
                 voice_id='qwen-user-20261028', session=None, runtime='baseline'):
        self.api_url = api_url.rstrip('/')
        self.timeout = float(timeout)
        self._voice_id = voice_id
        self._session = session or requests.Session()
        self.runtime = runtime

    @property
    def engine_name(self):
        return 'Qwen3-TTS 0.6B clone (' + ('CUDA graphs, streaming' if self.runtime == 'fast' else 'sentence buffered') + ')'

    @property
    def voice_id(self):
        return self._voice_id

    @property
    def should_buffer_sentence(self):
        return self.runtime != 'fast'

    def _post(self, path, text, *, stream=False):
        if not text.strip():
            raise ValueError('TTS text must not be blank')
        try:
            response = self._session.post(self.api_url + path,
                json={'text': text.strip(), 'voice_id': self.voice_id},
                timeout=(3.0, self.timeout), stream=stream)
        except requests.RequestException as exc:
            raise RuntimeError(f'Qwen TTS unavailable: {exc}') from exc
        if response.status_code != 200:
            try:
                raise RuntimeError(f'Qwen TTS HTTP {response.status_code}: {response.text[:300]}')
            finally:
                response.close()
        return response

    def synthesize(self, text):
        response = self._post('/tts', text)
        try:
            audio = response.content
            if not audio.startswith(b'RIFF'):
                raise RuntimeError('Qwen TTS returned invalid WAV audio')
            return audio
        finally:
            response.close()

    def synthesize_stream(self, text: str, cancel_event: threading.Event | None = None) -> Iterator[AudioChunk]:
        # The server returns a whole sentence. A receiver thread lets cancellation
        # discard pending audio immediately even while HTTP is waiting for synthesis.
        if cancel_event is not None and cancel_event.is_set():
            return
        response = self._post('/tts/stream', text, stream=True)
        try:
            headers = response.headers
            if headers.get('X-Voice-Id') != self.voice_id:
                raise RuntimeError('Qwen service voice does not match the selected profile')
            if headers.get('X-PCM-Format') != 'pcm_s16le':
                raise RuntimeError('Qwen service must return pcm_s16le')
            resampler = IncrementalPCMResampler(int(headers['X-Sample-Rate']),
                source_channels=int(headers['X-Channels']), bits_per_sample=int(headers['X-Bits-Per-Sample']))
            # Read on a daemon thread: no socket read can hold up a cancelled turn.
            import queue
            blocks = queue.Queue(maxsize=8)
            stopped = threading.Event()
            def put(item):
                while not stopped.is_set():
                    try:
                        blocks.put(item, timeout=0.05)
                        return
                    except queue.Full:
                        pass
            def receive():
                try:
                    for block in response.iter_content(chunk_size=4096):
                        if stopped.is_set():
                            break
                        if block:
                            put(block)
                    put(None)
                except Exception as exc:
                    put(exc)
                finally:
                    response.close()
            threading.Thread(target=receive, name='qwen-pcm-receiver', daemon=True).start()
            try:
                sequence, seen, completed = 0, False, False
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    try:
                        block = blocks.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if isinstance(block, Exception):
                        raise RuntimeError(f'Qwen PCM receive failed: {block}') from block
                    if block is None:
                        completed = True
                        resampler.feed(b'', final=True)
                        if not seen:
                            raise RuntimeError('Qwen TTS stream returned no PCM')
                        # An empty terminal marker avoids delaying the final PCM
                        # of every codec chunk just to discover the next chunk.
                        yield AudioChunk(b'', 16000, 1, 16, sequence, True)
                        return
                    pcm = resampler.feed(block)
                    if pcm:
                        seen = True
                        yield AudioChunk(pcm, 16000, 1, 16, sequence)
                        sequence += 1
            finally:
                stopped.set()
                request_id = headers.get('X-Request-Id')
                if request_id and not completed:
                    # A separate connection unblocks GPU generation at the next
                    # codec chunk even if the receive thread is waiting on a read.
                    try:
                        requests.post(self.api_url+'/tts/cancel', json={'request_id': request_id}, timeout=1)
                    except requests.RequestException:
                        pass
        finally:
            # Closing an actively blocked requests response can block as well;
            # leave that bounded read timeout to the receiver on cancellation.
            if 'stopped' not in locals():
                response.close()
