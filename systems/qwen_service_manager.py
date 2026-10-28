"""Qt lifecycle wrapper around the same Qwen companion used by the host."""
from PyQt6.QtCore import QObject, QThread, pyqtSignal
from host_service.qwen_service import QwenServiceController


class _Startup(QThread):
    ready = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def run(self):
        try:
            self.controller.start_and_wait()
            self.ready.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class QwenServiceManager(QObject):
    service_ready = pyqtSignal()
    service_failed = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.controller = QwenServiceController(config)
        self.worker = None

    def start_service(self):
        self.status_update.emit('Loading Qwen3-TTS cloned voice')
        self.worker = _Startup(self.controller)
        self.worker.ready.connect(self.service_ready.emit)
        self.worker.failed.connect(self.service_failed.emit)
        self.worker.start()

    def stop_service(self):
        if self.worker and self.worker.isRunning():
            self.worker.wait(3000)
        self.controller.stop()
