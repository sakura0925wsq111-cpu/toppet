"""Lifecycle manager for toppet's persistent local VoxCPM2 service."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from common.paths import LOGS_DIR, VOICE_REFERENCE_FILE


VOXCPM_ROOT = Path(r"D:\VoxCPM2")
DEFAULT_RUNTIME_PYTHON = VOXCPM_ROOT / ".venv" / "Scripts" / "python.exe"
DEFAULT_SERVER_SCRIPT = VOXCPM_ROOT / "service" / "voxcpm_server.py"
DEFAULT_MODEL_PATH = (
    VOXCPM_ROOT
    / "models"
    / "models"
    / "OpenBMB--VoxCPM2"
    / "snapshots"
    / "master"
)
DEFAULT_REFERENCE_PATH = VOICE_REFERENCE_FILE
DEFAULT_API_URL = "http://127.0.0.1:9890"
DEFAULT_PROMPT_TEXT = "今天真的有点忙，我本来以为很快就能弄完。"
DEFAULT_LOG_PATH = LOGS_DIR / "voxcpm_service.log"
DEFAULT_PID_PATH = LOGS_DIR / "voxcpm_service.pid"


def _logger() -> logging.Logger:
    logger = logging.getLogger("VoxCPMServiceManager")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(DEFAULT_LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


class VoxCPMStartupWorker(QThread):
    service_ready = pyqtSignal()
    service_failed = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def __init__(
        self,
        command: list[str],
        working_dir: Path,
        api_url: str,
        max_wait: float,
        poll_interval: float,
        logger: logging.Logger,
        pid_file: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.command = command
        self.working_dir = working_dir
        self.api_url = api_url.rstrip("/")
        self.max_wait = max_wait
        self.poll_interval = poll_interval
        self.log = logger
        self.pid_file = pid_file
        self.process: subprocess.Popen[str] | None = None
        self.cancelled = threading.Event()
        self._owned_process = False
        self._wrapper_exit_logged = False

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        try:
            if self._ready():
                self.status_update.emit("VoxCPM2 voice service connected")
                self.service_ready.emit()
                return
            self.status_update.emit("Loading and warming VoxCPM2 voice (about 1 minute)...")
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            process_env = os.environ.copy()
            process_env.setdefault("HF_HUB_OFFLINE", "1")
            process_env.setdefault("TRANSFORMERS_OFFLINE", "1")
            self.process = subprocess.Popen(
                self.command,
                cwd=str(self.working_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                env=process_env,
            )
            self._owned_process = True
            threading.Thread(target=self._drain_output, daemon=True).start()
            deadline = time.monotonic() + self.max_wait
            while time.monotonic() < deadline and not self.cancelled.is_set():
                if self._ready():
                    self.status_update.emit("VoxCPM2 cloned voice ready")
                    self.service_ready.emit()
                    return
                if self.process.poll() is not None and not self._wrapper_exit_logged:
                    # Windows venv launchers may hand off to the base Python
                    # process. The PID file remains the authoritative owner.
                    self._wrapper_exit_logged = True
                    self.log.info(
                        "Python launcher exited with code %s; waiting for service PID",
                        self.process.returncode,
                    )
                time.sleep(self.poll_interval)
            if self.cancelled.is_set():
                return
            raise TimeoutError(f"VoxCPM2 startup exceeded {self.max_wait:.0f}s")
        except Exception as exc:
            self.log.exception("VoxCPM2 startup failed")
            self.service_failed.emit(str(exc))

    def _ready(self) -> bool:
        try:
            response = requests.get(f"{self.api_url}/health", timeout=1.0)
            return (
                response.status_code == 200
                and response.json().get("status") == "ready"
            )
        except (requests.RequestException, ValueError):
            return False

    def _drain_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self.log.info("[service] %s", line)

    def stop_owned_process(self, timeout: float = 8.0) -> None:
        process = self.process
        if not self._owned_process:
            return
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            service_pid = int(self.pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return
        if service_pid == os.getpid():
            raise RuntimeError("Refusing to stop the toppet process")
        self.log.info("Stopping owned VoxCPM2 service PID=%s", service_pid)
        try:
            os.kill(service_pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        finally:
            self.pid_file.unlink(missing_ok=True)


class VoxCPMServiceManager(QObject):
    service_ready = pyqtSignal()
    service_failed = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        runtime_python: Path | str = DEFAULT_RUNTIME_PYTHON,
        server_script: Path | str = DEFAULT_SERVER_SCRIPT,
        model_path: Path | str = DEFAULT_MODEL_PATH,
        reference_path: Path | str = DEFAULT_REFERENCE_PATH,
        prompt_text: str = DEFAULT_PROMPT_TEXT,
        inference_steps: int = 4,
        max_wait: float = 150.0,
        poll_interval: float = 1.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.api_url = api_url.rstrip("/")
        self.runtime_python = Path(runtime_python)
        self.server_script = Path(server_script)
        self.model_path = Path(model_path)
        self.reference_path = Path(reference_path)
        self.prompt_text = prompt_text
        self.inference_steps = inference_steps
        self.max_wait = max_wait
        self.poll_interval = poll_interval
        self.log = _logger()
        self.worker: VoxCPMStartupWorker | None = None

    @property
    def runtime_available(self) -> bool:
        required = (
            self.runtime_python,
            self.server_script,
            self.model_path / "model.safetensors",
            self.model_path / "audiovae.pth",
            self.reference_path,
        )
        return all(path.is_file() for path in required)

    def is_service_running(self, timeout: float = 1.0) -> bool:
        try:
            response = requests.get(f"{self.api_url}/health", timeout=timeout)
            return (
                response.status_code == 200
                and response.json().get("status") == "ready"
            )
        except (requests.RequestException, ValueError):
            return False

    def start_service(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if not self.runtime_available and not self.is_service_running(timeout=0.25):
            self.service_failed.emit("VoxCPM2 runtime/model/reference files are incomplete")
            return
        parsed_port = self.api_url.rsplit(":", 1)[-1]
        try:
            port = int(parsed_port)
        except ValueError:
            port = 9890
        command = [
            str(self.runtime_python),
            "-u",
            str(self.server_script),
            "--model-path",
            str(self.model_path),
            "--reference-path",
            str(self.reference_path),
            "--prompt-text",
            self.prompt_text,
            "--port",
            str(port),
            "--steps",
            str(self.inference_steps),
            "--log-file",
            str(DEFAULT_LOG_PATH),
            "--pid-file",
            str(DEFAULT_PID_PATH),
        ]
        DEFAULT_PID_PATH.unlink(missing_ok=True)
        self.worker = VoxCPMStartupWorker(
            command=command,
            working_dir=self.server_script.parent,
            api_url=self.api_url,
            max_wait=self.max_wait,
            poll_interval=self.poll_interval,
            logger=self.log,
            pid_file=DEFAULT_PID_PATH,
        )
        self.worker.service_ready.connect(self.service_ready.emit)
        self.worker.service_failed.connect(self.service_failed.emit)
        self.worker.status_update.connect(self.status_update.emit)
        self.worker.start()

    def stop_service(self) -> None:
        worker = self.worker
        if worker is None:
            return
        worker.cancel()
        worker.stop_owned_process()
        if worker.isRunning():
            worker.wait(3000)
        self.worker = None
