"""Own one replaceable native ASR process so its failure cannot exit the host."""
from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading

from common.paths import PROJECT_ROOT


class IsolatedWhisperEngine:
    def __init__(self, model_size="small", device="cpu", compute_type="int8", *,
                 timeout=20.0, startup_timeout=60.0, command=None):
        self.model_size, self.device, self.compute_type = model_size, device, compute_type
        self.timeout, self.startup_timeout = timeout, startup_timeout
        self.command = command
        self._process = None
        self._lock = threading.RLock()
        self._messages = None

    @property
    def engine_name(self):
        return f"FasterWhisper ({self.model_size}, isolated)"

    def _receive(self, timeout):
        try:
            line = self._messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError("ASR worker timed out") from exc
        if line is None:
            raise RuntimeError("ASR worker exited before returning a result")
        result = json.loads(line)
        if result.get('error'):
            raise RuntimeError('ASR worker failed: ' + result['error'])
        return result

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        self._stop()
        command = self.command or [sys.executable, '-u', '-X', 'faulthandler', '-m',
            'audio.asr.whisper_worker', '--model', self.model_size, '--device', self.device,
            '--compute-type', self.compute_type]
        env = dict(os.environ, OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='2',
                   PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        self._process = subprocess.Popen(command, cwd=str(PROJECT_ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self._messages = queue.Queue()
        messages, stream = self._messages, self._process.stdout

        def read_lines():
            try:
                for line in stream:
                    messages.put(line)
            except (OSError, ValueError):
                pass  # Closing an owned failed worker can close this pipe.
            finally:
                messages.put(None)
        threading.Thread(target=read_lines, daemon=True, name='asr-worker-output').start()
        if not self._receive(self.startup_timeout).get('ready'):
            raise RuntimeError('ASR worker did not become ready')

    def _stop(self):
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()

    def preload(self):
        with self._lock:
            try:
                self._start()
            except Exception:
                self._stop()
                raise

    def transcribe_pcm(self, pcm: bytes, sample_rate=16000):
        if sample_rate != 16000 or not pcm or len(pcm) % 2:
            raise ValueError('ASR expects complete 16000 Hz signed 16-bit PCM')
        if len(pcm) > 320000:
            raise ValueError('ASR recording exceeds the 10 second device limit')
        with self._lock:
            try:
                self._start()
                request = json.dumps({'pcm': base64.b64encode(pcm).decode('ascii')}) + '\n'
                self._process.stdin.write(request.encode('utf-8'))
                self._process.stdin.flush()
                return self._receive(self.timeout)['text']
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                self._stop()
                # Do not manufacture a transcript or send a stale turn. A new
                # request creates a fresh worker; a failed request stays failed.
                raise RuntimeError('语音识别暂时失败，请再说一次：' + str(exc)) from exc

    def close(self):
        with self._lock:
            self._stop()
