# -*- coding: utf-8 -*-

from __future__ import annotations
import logging, os, subprocess, sys, time, threading
import requests
from PyQt6.QtCore import QThread, pyqtSignal, QObject

from common.paths import LOGS_DIR

DEFAULT_SOVITS_DIR = os.path.normpath(r"D:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604")
DEFAULT_RUNTIME_PYTHON = os.path.join(DEFAULT_SOVITS_DIR, "runtime", "python.exe")
DEFAULT_API_SCRIPT = os.path.join(DEFAULT_SOVITS_DIR, "api_v2.py")
DEFAULT_API_URL = "http://127.0.0.1:9880"
MAX_WAIT_SECONDS = 60
POLL_INTERVAL = 1.5
MAX_RETRY_COUNT = 2
RETRY_BACKOFF_BASE = 10.0
LOG_DIR = os.fspath(LOGS_DIR)


def _setup_service_logger(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "tts_service.log")
    logger = logging.getLogger("TTSServiceManager")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


class ServiceStartupWorker(QThread):
    service_ready = pyqtSignal()
    service_failed = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def __init__(self, api_url, startup_args, working_dir, max_wait=MAX_WAIT_SECONDS,
                 poll_interval=POLL_INTERVAL, logger=None, retry_count=MAX_RETRY_COUNT, parent=None):
        super().__init__(parent)
        self._api_url = api_url.rstrip("/")
        self._startup_args = startup_args
        self._working_dir = working_dir
        self._max_wait = max_wait
        self._poll_interval = poll_interval
        self._log = logger or _setup_service_logger(LOG_DIR)
        self._retry_count = retry_count
        self._process = None
        self._cancelled = False
        self._stdout_thread = None
        self._stderr_thread = None

    @property
    def process(self):
        return self._process

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self._try_launch()
        except Exception as exc:
            self._log.error(f"Startup exception: {exc}")
            self.service_failed.emit(f"Voice service startup failed: {exc}")

    # ------------------------------------------------------------------
    # Launch + retry logic
    # ------------------------------------------------------------------

    def _try_launch(self):
        self._log.info(f"Checking GPT-SoVITS API: {self._api_url}")
        if self._check_api():
            self._log.info("GPT-SoVITS API already running, skip launch")
            self.status_update.emit("Voice system connected")
            time.sleep(2.0)
            self.service_ready.emit()
            return

        remaining = self._retry_count + 1
        for attempt in range(remaining):
            if self._cancelled:
                self._log.warning("Startup cancelled")
                return
            if attempt > 0:
                wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                self._log.warning(f"Retry {attempt}/{self._retry_count} after {wait:.0f}s backoff...")
                self.status_update.emit(f"Retrying ({attempt}/{self._retry_count})...")
                time.sleep(wait)
            if self._start_and_wait():
                return
            # subprocess died or never came up; log exit detail
            if self._process is not None:
                exit_code = self._process.poll()
                if exit_code is not None:
                    self._log.error(f"Subprocess exited with code {exit_code} before API ready")
                    self._capture_remaining_stderr()
            self._terminate_pipes()
            self._process = None

        self._log.error(f"API startup failed after {remaining} attempt(s)")
        self.service_failed.emit(f"Voice service startup failed ({remaining} attempts)")

    def _start_and_wait(self):
        self._log.info("GPT-SoVITS API not running, starting...")
        self.status_update.emit("Starting voice core...")
        self._start_process()
        self._log.info(f"Waiting for API (max {self._max_wait}s)...")
        self.status_update.emit("Connecting to voice service...")
        elapsed = 0.0
        while elapsed < self._max_wait:
            if self._cancelled:
                self._log.warning("Startup cancelled")
                return False
            # check if subprocess died early
            if self._process is not None and self._process.poll() is not None:
                exit_code = self._process.poll()
                self._log.error(f"Subprocess exited early with code {exit_code} after {elapsed:.1f}s")
                self._capture_remaining_stderr()
                return False
            if self._check_api():
                self._log.info(f"API port open ({elapsed:.1f}s), waiting for init...")
                self.status_update.emit("Waiting for voice system (5s)...")
                time.sleep(5.0)
                self._log.info("API warmup done")
                self.status_update.emit("Voice system connected")
                self.service_ready.emit()
                return True
            time.sleep(self._poll_interval)
            elapsed += self._poll_interval
        self._log.error(f"API startup timeout ({self._max_wait}s)")
        return False

    # ------------------------------------------------------------------
    # API health check
    # ------------------------------------------------------------------

    def _check_api(self):
        try:
            r = requests.get(self._api_url, timeout=3)
            return True
        except requests.ConnectionError:
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------

    def _start_process(self):
        cmd = [str(arg) for arg in self._startup_args]
        cwd = str(self._working_dir)
        self._log.info(f"Exec: {' '.join(cmd)}")
        self._log.info(f"CWD: {cwd}")
        # DETACHED_PROCESS (0x00000008): detaches from parent console but
        # allows the child to create its own. Safer than CREATE_NO_WINDOW
        # which removes console handles that GPU/CUDA init may need.
        creationflags = 0x00000008 if sys.platform == "win32" else 0
        try:
            self._process = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=creationflags, text=True, encoding="utf-8", errors="replace")
            self._log.info(f"Process started, PID={self._process.pid}")
            # Background readers so pipes never fill up
            self._stdout_thread = threading.Thread(
                target=self._read_pipe, args=(self._process.stdout, "STDOUT"), daemon=True)
            self._stderr_thread = threading.Thread(
                target=self._read_pipe, args=(self._process.stderr, "STDERR"), daemon=True)
            self._stdout_thread.start()
            self._stderr_thread.start()
        except FileNotFoundError as exc:
            raise RuntimeError(f"CMD: {' '.join(cmd)}, CWD: {cwd}, Error: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Failed to start subprocess: {exc}") from exc

    def _read_pipe(self, pipe, label):
        """Background thread: drain subprocess pipe into log."""
        try:
            for line in pipe:
                line = line.rstrip()
                if line:
                    self._log.debug(f"[{label}] {line}")
        except Exception:
            pass

    def _capture_remaining_stderr(self):
        """Read leftover stderr after subprocess exit for diagnosis."""
        try:
            if self._process is not None and self._process.stderr is not None:
                while True:
                    line = self._process.stderr.readline()
                    if not line:
                        break
                    line = line.rstrip()
                    if line:
                        self._log.error(f"[STDERR-EXIT] {line}")
        except Exception:
            pass

    def _terminate_pipes(self):
        for thr in (self._stdout_thread, self._stderr_thread):
            if thr is not None and thr.is_alive():
                thr.join(timeout=1.0)
        self._stdout_thread = None
        self._stderr_thread = None

    def terminate_process(self, timeout=5.0):
        if self._process is None:
            return
        pid = self._process.pid
        self._log.info(f"Shutting down GPT-SoVITS subprocess PID={pid}...")
        self._terminate_pipes()
        try:
            self._process.terminate()
            self._process.wait(timeout=timeout)
            self._log.info(f"Subprocess PID={pid} exited normally")
        except subprocess.TimeoutExpired:
            self._log.warning(f"Subprocess PID={pid} did not exit, force kill")
            self._process.kill()
            self._process.wait()
            self._log.info(f"Subprocess PID={pid} killed")
        except Exception as exc:
            self._log.error(f"Error closing subprocess: {exc}")
        finally:
            self._process = None


class TTSServiceManager(QObject):
    service_ready = pyqtSignal()
    service_failed = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def __init__(self, api_url=DEFAULT_API_URL, sovits_dir=None, runtime_python=None,
                 api_script=None, max_wait=MAX_WAIT_SECONDS, poll_interval=POLL_INTERVAL,
                 retry_count=MAX_RETRY_COUNT, log_dir=None, parent=None):
        super().__init__(parent)
        self._api_url = api_url.rstrip("/")
        self._runtime_python = runtime_python or DEFAULT_RUNTIME_PYTHON
        self._api_script = api_script or DEFAULT_API_SCRIPT
        self._max_wait = max_wait
        self._poll_interval = poll_interval
        self._retry_count = retry_count
        self._log_dir = log_dir or LOG_DIR
        self._logger = _setup_service_logger(self._log_dir)
        self._worker = None
        self._working_dir = os.path.dirname(self._api_script)
        self._startup_args = [
            self._runtime_python, "-u", self._api_script,
            "-a", "127.0.0.1", "-p", "9880",
            "-c", os.path.join(self._working_dir, "GPT_SoVITS", "configs", "tts_infer.yaml"),
        ]
        self.gpt_model_rel = "GPT_weights_v2Pro/吴绍强-e15.ckpt"
        self.sovits_model_rel = "SoVITS_weights_v2pro/吴绍强_e8_s160.pth"

    # -- Public API --

    @property
    def runtime_available(self):
        """Whether the configured local GPT-SoVITS runtime can be started."""
        return (
            os.path.isfile(self._runtime_python)
            and os.path.isfile(self._api_script)
        )

    def start_service(self):
        if self._worker is not None and self._worker.isRunning():
            self._logger.info("start_service: worker already running, skip")
            return
        self._logger.info("========== TTSServiceManager startup ==========")
        self._worker = ServiceStartupWorker(
            api_url=self._api_url, startup_args=self._startup_args,
            working_dir=self._working_dir, max_wait=self._max_wait,
            poll_interval=self._poll_interval, logger=self._logger,
            retry_count=self._retry_count)
        self._worker.service_ready.connect(self._on_worker_ready)
        self._worker.service_failed.connect(self._on_worker_failed)
        self._worker.status_update.connect(self._on_worker_status)
        self._worker.start()

    def stop_service(self):
        self._logger.info("TTSServiceManager.stop_service() called")
        worker = self._worker
        if worker is not None:
            worker.cancel()
            if worker.process is not None:
                worker.terminate_process(timeout=3.0)
            if worker.isRunning():
                worker.quit()
                worker.wait(3000)
            self._worker = None
            self._logger.info("GPT-SoVITS subprocess closed")

    def is_service_running(self, timeout=2.0):
        try:
            r = requests.get(self._api_url, timeout=timeout)
            return r.status_code < 500
        except Exception:
            return False

    def wait_until_ready(self, timeout=None):
        max_t = timeout if timeout is not None else self._max_wait
        elapsed = 0.0
        while elapsed < max_t:
            if self.is_service_running():
                return True
            time.sleep(self._poll_interval)
            elapsed += self._poll_interval
        return False

    # -- Internal signal bridging --

    def _on_worker_ready(self):
        self._logger.info("Service ready -> service_ready.emit()")
        self.service_ready.emit()

    def _on_worker_failed(self, msg):
        self._logger.error(f"Service failed: {msg}")
        self.service_failed.emit(msg)

    def _on_worker_status(self, msg):
        self.status_update.emit(msg)
