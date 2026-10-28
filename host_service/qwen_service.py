"""Lifecycle controller for the local Qwen runtime, independent of Qt."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import socket
import threading
import time
from urllib.parse import urlparse

import requests

from common.paths import LOGS_DIR, PROJECT_ROOT


class QwenServiceController:
    def __init__(self, config=None, *, max_wait=120.0):
        config = config or {}
        self.root = Path(config.get('runtime_root', r'D:\Qwen3TTS'))
        self.voice_dir = Path(config.get('voice_dir', self.root/'voices'/'user-20261028'))
        self.api_url = config.get('api_url', 'http://127.0.0.1:9891').rstrip('/')
        self.voice_id = config.get('voice', 'qwen-user-20261028')
        self.runtime = config.get('qwen_runtime', os.getenv('TOPPET_QWEN_RUNTIME', 'baseline'))
        if self.runtime not in {'baseline', 'fast'}:
            raise ValueError(f'Unknown Qwen runtime: {self.runtime}')
        parsed = urlparse(self.api_url)
        if parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1'} or not parsed.port:
            raise ValueError('Qwen companion must use a localhost HTTP URL with an explicit port')
        self.port = parsed.port
        self.max_wait = max_wait
        self.process = None
        self._log = None
        self._owned = False
        self._stopping = threading.Event()
        self.pid_path = LOGS_DIR/f'qwen-{self.port}.pid'

    def is_ready(self):
        try:
            r = requests.get(self.api_url+'/health', timeout=1)
            d = r.json()
            return (r.status_code == 200 and d.get('status') == 'ready'
                    and d.get('service') == 'toppet-qwen-tts' and d.get('voice_id') == self.voice_id
                    and d.get('inference_backend', 'baseline') == self.runtime
                    and (self.runtime != 'fast' or d.get('decoder_mode') == 'full-context-prefix'))
        except (requests.RequestException, ValueError):
            return False

    def start_and_wait(self):
        if self._stopping.is_set():
            return
        if self.is_ready():
            return
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(('127.0.0.1', self.port)) == 0:
                raise RuntimeError(f'Port {self.port} already serves a different voice or service')
        python = self.root/('.venv-fast' if self.runtime == 'fast' else '.venv')/'Scripts'/'python.exe'
        model = self.root/'models'/'Qwen3-TTS-12Hz-0.6B-Base'
        for path in [python, model/'model.safetensors', model/'speech_tokenizer'/'model.safetensors',
                     self.voice_dir/'voice-prompt.pt', self.voice_dir/'reference.txt', self.voice_dir/'voice.json']:
            if not path.is_file():
                raise RuntimeError(f'Qwen runtime file missing: {path}')
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._log = (LOGS_DIR/'qwen_service.log').open('a', encoding='utf-8')
        env = os.environ.copy()
        env.update(PYTHONIOENCODING='utf-8', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        self.process = subprocess.Popen([str(python), '-u', str(PROJECT_ROOT/'host_service'/'qwen_server.py'),
            '--model', str(model), '--voice-dir', str(self.voice_dir), '--voice-id', self.voice_id,
            '--port', str(self.port), '--pid-file', str(self.pid_path), '--runtime', self.runtime], cwd=str(PROJECT_ROOT),
            env=env, stdout=self._log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self._owned = True
        deadline = time.monotonic()+self.max_wait
        while time.monotonic() < deadline and not self._stopping.is_set():
            if self.is_ready():
                return
            # venv launchers can exit before their real Python child.
            if self.process.poll() not in (None, 0):
                break
            time.sleep(0.25)
        self.stop()
        raise RuntimeError(f'Qwen startup failed; see {LOGS_DIR / "qwen_service.log"}')

    def stop(self):
        self._stopping.set()
        if not self._owned:
            return
        # Stop only the child from this launch. The PID file is written by that
        # server after loading; compare with our process tree before using it.
        if os.name == 'nt' and self.process and self.process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        elif self.process and self.process.poll() is None:
            self.process.terminate()
        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._owned = False
        if self._log:
            self._log.close()
            self._log = None
