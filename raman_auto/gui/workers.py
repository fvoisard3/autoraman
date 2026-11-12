"""Exécution des appels aux appareils dans un thread dédié par appareil.

LightField impose que l'objet Automation reste dans un seul thread ; on applique la même règle
à la platine. L'interface soumet des fonctions et reçoit le résultat par signal.
"""
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot


class _Executor(QObject):
    done = Signal(str, object)
    failed = Signal(str, str)

    @Slot(str, object)
    def execute(self, tag, fn):
        try:
            result = fn()
        except Exception as e:  # toute erreur d'appareil est rapportée à l'interface
            self.failed.emit(tag, str(e))
        else:
            self.done.emit(tag, result)


class DeviceThread(QObject):
    done = Signal(str, object)
    failed = Signal(str, str)
    busy_changed = Signal(bool)
    _request = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending = 0
        self._thread = QThread(self)
        self._executor = _Executor()
        self._executor.moveToThread(self._thread)
        self._request.connect(self._executor.execute)
        self._executor.done.connect(self._on_done)
        self._executor.failed.connect(self._on_failed)
        self._thread.start()

    @property
    def busy(self):
        return self._pending > 0

    def submit(self, tag, fn):
        self._pending += 1
        if self._pending == 1:
            self.busy_changed.emit(True)
        self._request.emit(tag, fn)

    def _finish_one(self):
        self._pending -= 1
        if self._pending == 0:
            self.busy_changed.emit(False)

    @Slot(str, object)
    def _on_done(self, tag, result):
        self._finish_one()
        self.done.emit(tag, result)

    @Slot(str, str)
    def _on_failed(self, tag, message):
        self._finish_one()
        self.failed.emit(tag, message)

    def shutdown(self, close_fn=None, timeout_s=20.0):
        """Exécute close_fn dans le thread de l'appareil, puis arrête le thread."""
        self._executor.done.disconnect(self._on_done)  # plus rien à afficher à la fermeture
        self._executor.failed.disconnect(self._on_failed)
        if close_fn is not None:
            finished = threading.Event()

            def job():
                try:
                    close_fn()
                finally:
                    finished.set()

            self._request.emit("shutdown", job)
            finished.wait(timeout_s)
        self._thread.quit()
        self._thread.wait(int(timeout_s * 1000))
