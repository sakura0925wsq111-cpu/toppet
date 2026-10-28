"""Local Qwen companion, with baseline and CUDA-graph streaming runtimes."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import io
import json
import logging
import os
import queue
from pathlib import Path
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
if __package__:
    from .qwen_streaming import stable_voice_clone_stream
else:
    from qwen_streaming import stable_voice_clone_stream


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    voice_id: str = 'qwen-user-20261028'
    seed: int = 42
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=100)


class CancelRequest(BaseModel):
    request_id: str


def build_app(args):
    lock = threading.Lock()
    pending = {}
    pending_lock = threading.Lock()
    backend = getattr(args, 'runtime', 'baseline')
    state = {'status': 'loading', 'service': 'toppet-qwen-tts', 'voice_id': args.voice_id,
             'sample_rate': 24000, 'inference_backend': backend,
             'decoder_mode': 'full-context-prefix' if backend == 'fast' else 'upstream',
             'streaming_mode': 'codec-chunks' if backend == 'fast' else 'sentence-buffered', 'request_count': 0}
    runtime = {}

    @asynccontextmanager
    async def lifespan(app):
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        import torch
        from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem
        torch.set_num_threads(4)
        if backend == 'fast':
            from faster_qwen3_tts import FasterQwen3TTS
            model = FasterQwen3TTS.from_pretrained(str(args.model), device='cuda:0', dtype=torch.bfloat16,
                attn_implementation='sdpa', max_seq_len=1024, local_files_only=True)
        else:
            from qwen_tts import Qwen3TTSModel
            model = Qwen3TTSModel.from_pretrained(str(args.model), device_map='cuda:0', dtype=torch.bfloat16,
                                                attn_implementation='sdpa', local_files_only=True)
        saved = torch.load(args.voice_dir/'voice-prompt.pt', map_location='cuda:0', weights_only=True)
        profile = json.loads((args.voice_dir/'voice.json').read_text(encoding='utf-8'))
        if (saved['reference_text'] != (args.voice_dir/'reference.txt').read_text(encoding='utf-8').strip()
                or saved['source_sha256'] != profile['source_sha256']):
            raise RuntimeError('Saved voice prompt does not match the reference profile')
        runtime.update(model=model, prompt=[VoiceClonePromptItem(**item) for item in saved['items']])
        if backend == 'fast':
            # Capture graphs and warm the actual ICL/codec path before accepting turns.
            for _audio, _sr, _timing in stable_voice_clone_stream(model, text='在呢，咋了。',
                    voice_clone_prompt=runtime['prompt'], max_new_tokens=64, chunk_size=8):
                pass
        state['status'] = 'ready'
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text(str(os.getpid()), encoding='ascii')
        try:
            yield
        finally:
            state['status'] = 'stopped'
            if args.pid_file.exists() and args.pid_file.read_text().strip() == str(os.getpid()):
                args.pid_file.unlink()
            runtime.clear()

    app = FastAPI(lifespan=lifespan)

    def pcm_parts(req, cancelled):
        import numpy as np
        import torch
        while not lock.acquire(timeout=0.05):
            if cancelled.is_set():
                return None
        try:
            if cancelled.is_set():
                return None
            torch.manual_seed(req.seed)
            started = time.perf_counter()
            if backend == 'fast':
                source = stable_voice_clone_stream(runtime['model'], text=req.text,
                    voice_clone_prompt=runtime['prompt'], max_new_tokens=512, chunk_size=8, cancel_event=cancelled)
            else:
                def single():
                    waves, sr = runtime['model'].generate_voice_clone(text=req.text, language='Chinese',
                        voice_clone_prompt=runtime['prompt'], max_new_tokens=512)
                    yield waves[0], sr, {}
                source = single()
            count, duration = 0, 0.0
            try:
                for wave, sr, _timing in source:
                    if cancelled.is_set():
                        break
                    audio = np.asarray(wave, dtype=np.float32)
                    if not audio.size:
                        continue
                    if not np.isfinite(audio).all() or sr != 24000:
                        raise RuntimeError('Invalid Qwen audio or sample rate')
                    if count == 0:
                        state['last_first_audio_seconds'] = round(time.perf_counter()-started, 3)
                    count += 1
                    duration += len(audio)/sr
                    yield (np.clip(audio, -1, 1)*32767).astype('<i2').tobytes()
            finally:
                source.close()
                state.update(request_count=state['request_count']+1,
                    last_inference_seconds=round(time.perf_counter()-started, 3),
                    last_audio_seconds=duration, last_chunk_count=count, last_cancelled=cancelled.is_set())
        finally:
            lock.release()

    def validate(req):
        if req.voice_id != args.voice_id:
            raise HTTPException(409, 'Requested voice is not loaded')
        if not req.text.strip():
            raise HTTPException(422, 'Speech text must not be blank')

    @app.get('/health')
    def health():
        return dict(state)

    @app.post('/tts')
    def wav(req: SpeechRequest):
        import wave
        validate(req)
        pcm = b''.join(pcm_parts(req, threading.Event()))
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(pcm)
        return Response(buf.getvalue(), media_type='audio/wav')

    @app.post('/tts/stream')
    async def pcm(req: SpeechRequest):
        validate(req)
        cancelled = threading.Event()
        with pending_lock:
            if req.request_id in pending:
                raise HTTPException(409, 'Request ID already active')
            pending[req.request_id] = cancelled
        async def body():
            blocks = queue.Queue(maxsize=8)
            sentinel = object()
            def put(item):
                while not cancelled.is_set():
                    try:
                        blocks.put(item, timeout=0.05)
                        return
                    except queue.Full:
                        pass
            def produce():
                source = pcm_parts(req, cancelled)
                try:
                    for block in source:
                        if cancelled.is_set():
                            break
                        put(block)
                    put(sentinel)
                except Exception as exc:
                    state['last_error'] = str(exc)
                    put(exc)
                finally:
                    source.close()
                    with pending_lock:
                        pending.pop(req.request_id, None)
            threading.Thread(target=produce, name='qwen-generate', daemon=True).start()
            try:
                while not cancelled.is_set():
                    try:
                        block = await asyncio.to_thread(blocks.get, True, 0.05)
                    except queue.Empty:
                        continue
                    if block is sentinel:
                        break
                    if isinstance(block, Exception):
                        raise block
                    yield block
            finally:
                cancelled.set()
        return StreamingResponse(body(), media_type='application/octet-stream', headers={
            'X-Sample-Rate': '24000', 'X-Channels': '1', 'X-Bits-Per-Sample': '16',
            'X-PCM-Format': 'pcm_s16le', 'X-Voice-Id': args.voice_id,
            'X-Request-Id': req.request_id,
            'X-Streaming-Mode': state['streaming_mode']})

    @app.post('/tts/cancel')
    def cancel(req: CancelRequest):
        with pending_lock:
            event = pending.get(req.request_id)
            if event is not None:
                event.set()
        return {'cancelled': event is not None}
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--voice-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=9891)
    parser.add_argument('--voice-id', default='qwen-user-20261028')
    parser.add_argument('--pid-file', type=Path, required=True)
    parser.add_argument('--runtime', choices=['baseline', 'fast'], default='baseline')
    args = parser.parse_args()
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(build_app(args), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
