"""Tiny QThread wrapper: run a function off the GUI thread."""

import threading
import traceback

from qgis.PyQt.QtCore import QThread, pyqtSignal

from .downloader import Cancelled

# Workers detached from their owner are parked here so Python keeps a
# reference until the thread really finishes. Destroying a QThread while
# its thread is still running aborts the whole process, which previously
# could happen when the dialog was closed during a long, non-interruptible
# GDAL step (stop() timed out, dialog was destroyed, worker went with it).
_detached = []
_detached_lock = threading.Lock()


def _release_detached(worker):
    with _detached_lock:
        try:
            _detached.remove(worker)
        except ValueError:
            pass
    worker.deleteLater()


class Worker(QThread):
    progress = pyqtSignal(int, str)   # percent, message
    item = pyqtSignal(object)         # arbitrary per-item payload
    succeeded = pyqtSignal(object)    # result of fn
    failed = pyqtSignal(str)          # error text ("" == cancelled)

    def __init__(self, fn, parent=None):
        """fn(worker) -> result. Use worker.report/emit_item/is_cancelled."""
        super().__init__(parent)
        self._fn = fn
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def is_cancelled(self):
        return self._cancel.is_set()

    def report(self, percent, message=""):
        self.progress.emit(int(percent), message)

    def emit_item(self, payload):
        self.item.emit(payload)

    def run(self):
        try:
            result = self._fn(self)
        except Cancelled:
            self.failed.emit("")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")
        else:
            if self.is_cancelled():
                self.failed.emit("")
            else:
                self.succeeded.emit(result)

    def stop(self, timeout_ms=5000):
        """Cancel and wait for the thread to finish (call from GUI thread).

        Returns True when the thread has finished. If False is returned the
        caller must call detach() rather than simply dropping the last
        reference (see detach).
        """
        if self.isRunning():
            self.cancel()
            return bool(self.wait(timeout_ms))
        return True

    def detach(self):
        """Park a still-running worker so nothing destroys it too early.

        Disconnects all signals (results are no longer wanted), removes the
        parent link and keeps a module-level reference until the thread
        finishes, then deletes the object. Callers use this after
        stop(timeout_ms) returned False.
        """
        if not self.isRunning():
            return
        try:
            self.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.setParent(None)
        with _detached_lock:
            _detached.append(self)
        self.finished.connect(lambda *_: _release_detached(self))
