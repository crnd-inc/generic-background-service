import time
import threading

from odoo.tests.common import TransactionCase

from odoo.addons.generic_background_service.service.background_service import (
    BackgroundService,
    DatabaseProbe,
)


class DummyWorker(threading.Thread):
    """Minimal worker stub for testing BackgroundService."""

    def __init__(self, service_name, dbname, params):
        super().__init__(
            name="DummyWorker-%s-%s" % (service_name, dbname))
        self._stopped = threading.Event()
        self._wakeup = threading.Event()

    def run(self):
        while not self._stopped.is_set():
            self._wakeup.wait(1.0)
            self._wakeup.clear()

    def worker_stop(self):
        self._stopped.set()
        self._wakeup.set()

    def worker_is_stopped(self):
        return self._stopped.is_set()

    def wakeup(self):
        self._wakeup.set()


class DummyBackgroundService(BackgroundService):
    _name = None  # Avoid auto-registration via metaclass

    def get_worker_class(self):
        return DummyWorker

    def _db_list(self):
        return []

    def _check_is_db_active(self, dbname):
        return DatabaseProbe(dbname, True, 'ok')


class TestBackgroundServiceWakeupEvent(TransactionCase):
    """Test that BackgroundService.sleep() correctly resets
    the wakeup event after being woken up, so subsequent
    sleep() calls still block for the full beat timeout.

    Bug: _service_event_wakeup is never cleared after wait(),
    so once wakeup() is called, sleep() becomes a no-op forever.
    """

    def test_sleep_blocks_after_wakeup(self):
        service = DummyBackgroundService()
        service._beat_timeout = 0.5

        # First, trigger a wakeup
        service.wakeup()

        # sleep() should return quickly because wakeup is set
        t0 = time.monotonic()
        service.sleep()
        elapsed1 = time.monotonic() - t0
        self.assertLess(elapsed1, 0.1,
                        "First sleep after wakeup should return immediately")

        # Now sleep() should block again for ~beat_timeout,
        # because the wakeup event should have been cleared.
        t0 = time.monotonic()
        service.sleep()
        elapsed2 = time.monotonic() - t0
        self.assertGreaterEqual(
            elapsed2, 0.4,
            "Second sleep should block for beat_timeout, "
            "but wakeup event was not cleared (busy-spin bug)")
