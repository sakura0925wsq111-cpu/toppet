"""Own the optional VoxCPM2 process used by the hardware host service.

The desktop UI has a Qt-based lifecycle manager.  The FastAPI host service
must not depend on a Qt event loop, so it uses this small synchronous
controller from its application lifespan instead.
"""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import TextIO

import requests

from common.paths import LOGS_DIR, VOICE_REFERENCE_FILE


logger = logging.getLogger(__name__)

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
DEFAULT_API_URL = "http://127.0.0.1:9890"
DEFAULT_PROMPT_TEXT = "今天真的有点忙，我本来以为很快就能弄完。"
DEFAULT_LOG_PATH = LOGS_DIR / "voxcpm_service.log"
DEFAULT_PID_PATH = LOGS_DIR / "voxcpm_service.pid"


class VoxCPMStartupError(RuntimeError):
    """Raised when the host service cannot prepare its cloned-voice service."""


class VoxCPMServiceController:
    """Start VoxCPM2 when needed and stop only the process started here."""

    def __init__(
        self,
        *,
        api_url: str = DEFAULT_API_URL,
        runtime_python: Path | str = DEFAULT_RUNTIME_PYTHON,
        server_script: Path | str = DEFAULT_SERVER_SCRIPT,
        model_path: Path | str = DEFAULT_MODEL_PATH,
        reference_path: Path | str = VOICE_REFERENCE_FILE,
        prompt_text: str = DEFAULT_PROMPT_TEXT,
        inference_steps: int = 4,
        max_wait: float = 180.0,
        poll_interval: float = 1.0,
        log_path: Path | str = DEFAULT_LOG_PATH,
        pid_path: Path | str = DEFAULT_PID_PATH,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.runtime_python = Path(runtime_python)
        self.server_script = Path(server_script)
        self.model_path = Path(model_path)
        self.reference_path = Path(reference_path)
        self.prompt_text = prompt_text
        self.inference_steps = inference_steps
        self.max_wait = max_wait
        self.poll_interval = poll_interval
        self.log_path = Path(log_path)
        self.pid_path = Path(pid_path)
        self.process: subprocess.Popen[str] | None = None
        self._owned_process = False
        self._log_stream: TextIO | None = None

    def is_ready(self, timeout: float = 1.0) -> bool:
        try:
            response = requests.get(f"{self.api_url}/health", timeout=timeout)
            return (
                response.status_code == 200
                and response.json().get("status") == "ready"
            )
        except (requests.RequestException, ValueError):
            return False

    def _missing_runtime_paths(self) -> list[Path]:
        required = (
            self.runtime_python,
            self.server_script,
            self.model_path / "model.safetensors",
            self.model_path / "audiovae.pth",
            self.reference_path,
        )
        return [path for path in required if not path.is_file()]

    def start_and_wait(self) -> None:
        """Ensure VoxCPM2 is ready before the host begins accepting requests."""
        if self.is_ready():
            logger.info("Using an existing VoxCPM2 service at %s", self.api_url)
            return

        missing = self._missing_runtime_paths()
        if missing:
            paths = ", ".join(str(path) for path in missing)
            raise VoxCPMStartupError(f"VoxCPM2 runtime is incomplete: {paths}")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        existing_pid = self._read_service_pid()
        if existing_pid is not None and self._pid_is_running(existing_pid):
            logger.info(
                "Waiting for existing VoxCPM2 process PID=%s to finish warming",
                existing_pid,
            )
            self._wait_for_existing_service(existing_pid)
            return
        self.pid_path.unlink(missing_ok=True)
        self._log_stream = self.log_path.open("a", encoding="utf-8")

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
            "--host",
            "127.0.0.1",
            "--port",
            self.api_url.rsplit(":", 1)[-1],
            "--steps",
            str(self.inference_steps),
            "--pid-file",
            str(self.pid_path),
        ]
        creationflags = (
            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        process_env = os.environ.copy()
        process_env.setdefault("HF_HUB_OFFLINE", "1")
        process_env.setdefault("TRANSFORMERS_OFFLINE", "1")

        logger.info("Starting and warming VoxCPM2; this may take about one minute")
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(self.server_script.parent),
                stdout=self._log_stream,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                env=process_env,
            )
        except Exception as exc:
            self._close_log_stream()
            raise VoxCPMStartupError(f"Unable to start VoxCPM2: {exc}") from exc
        self._owned_process = True

        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            if self.is_ready():
                logger.info("VoxCPM2 cloned voice is ready")
                return
            if (
                self.process.poll() is not None
                and not self.pid_path.exists()
            ):
                return_code = self.process.returncode
                self.stop()
                raise VoxCPMStartupError(
                    f"VoxCPM2 exited during startup with code {return_code}; "
                    f"see {self.log_path}"
                )
            time.sleep(self.poll_interval)

        self.stop()
        raise VoxCPMStartupError(
            f"VoxCPM2 did not become ready within {self.max_wait:.0f}s; "
            f"see {self.log_path}"
        )

    def stop(self) -> None:
        """Stop only a VoxCPM2 process launched by this controller."""
        if not self._owned_process:
            return

        service_pid = self._read_service_pid()
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()

        if service_pid is not None and service_pid != os.getpid():
            try:
                os.kill(service_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        self.pid_path.unlink(missing_ok=True)
        self._owned_process = False
        self.process = None
        self._close_log_stream()

    def _read_service_pid(self) -> int | None:
        try:
            return int(self.pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                ):
                    return False
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def _wait_for_existing_service(self, pid: int) -> None:
        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            if self.is_ready():
                logger.info("Existing VoxCPM2 cloned voice is ready")
                return
            if not self._pid_is_running(pid):
                self.pid_path.unlink(missing_ok=True)
                raise VoxCPMStartupError(
                    f"Existing VoxCPM2 process PID={pid} exited during startup; "
                    f"see {self.log_path}"
                )
            time.sleep(self.poll_interval)
        raise VoxCPMStartupError(
            f"Existing VoxCPM2 process PID={pid} did not become ready within "
            f"{self.max_wait:.0f}s; see {self.log_path}"
        )

    def _close_log_stream(self) -> None:
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None
