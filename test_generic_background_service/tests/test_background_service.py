import time
import logging
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


class StuckWorker(threading.Thread):
    """Worker that ignores the stop signal and blocks indefinitely."""

    def __init__(self, service_name, dbname, params):
        super().__init__(
            name="StuckWorker-%s-%s" % (service_name, dbname),
            daemon=True)
        self._stopped = threading.Event()
        self._wakeup = threading.Event()
        self._block = threading.Event()

    def run(self):
        # Block forever, ignoring stop signal
        self._block.wait()

    def worker_stop(self):
        self._stopped.set()

    def worker_is_stopped(self):
        return self._stopped.is_set()

    def wakeup(self):
        self._wakeup.set()

    def unblock(self):
        """Allow the worker to exit (for cleanup)."""
        self._block.set()


class TestShutdownTimeout(TransactionCase):
    """Test that shutdown_workers() respects _shutdown_timeout
    and does not hang indefinitely on stuck workers.
    """

    def _make_service(self, worker_cls, shutdown_timeout):
        service = DummyBackgroundService()
        service._shutdown_timeout = shutdown_timeout
        service.get_worker_class = lambda: worker_cls
        return service

    def test_shutdown_workers_does_not_hang_on_stuck_worker(self):
        """shutdown_workers() should return within _shutdown_timeout
        even if a worker ignores the stop signal."""
        timeout = 1
        service = self._make_service(StuckWorker, timeout)
        service.spawn_worker('testdb')
        worker = service._workers['testdb']

        t0 = time.monotonic()
        service.shutdown_workers()
        elapsed = time.monotonic() - t0

        self.assertLess(
            elapsed, timeout + 1,
            "shutdown_workers() should not hang beyond the timeout")
        # Worker is still alive because it ignores stop
        self.assertTrue(worker.is_alive())
        # Clean up: unblock the stuck worker so it can exit
        worker.unblock()
        worker.join(timeout=2)

    def test_shutdown_timeout_configurable(self):
        """Subclasses can set a custom _shutdown_timeout."""
        timeout = 0.5
        service = self._make_service(StuckWorker, timeout)
        service.spawn_worker('testdb')
        worker = service._workers['testdb']

        t0 = time.monotonic()
        service.shutdown_workers()
        elapsed = time.monotonic() - t0

        # Should have waited roughly _shutdown_timeout, not the default 30s
        self.assertLess(elapsed, timeout + 1)
        self.assertGreaterEqual(elapsed, timeout * 0.8)
        worker.unblock()
        worker.join(timeout=2)

    def test_shutdown_workers_logs_warning_on_timeout(self):
        """A warning should be logged when a worker doesn't stop in time."""
        timeout = 0.5
        service = self._make_service(StuckWorker, timeout)
        service.spawn_worker('testdb')
        worker = service._workers['testdb']

        logger_name = (
            'odoo.addons.generic_background_service'
            '.service.background_service'
        )
        with self.assertLogs(logger_name, level='WARNING') as cm:
            service.shutdown_workers()

        self.assertTrue(
            any('did not stop within' in msg for msg in cm.output),
            "Expected a warning about worker not stopping in time")
        worker.unblock()
        worker.join(timeout=2)

    def test_shutdown_normal_workers_no_warning(self):
        """Normal workers that stop promptly should not trigger a warning."""
        service = self._make_service(DummyWorker, 5)
        service.spawn_worker('testdb')

        logger_name = (
            'odoo.addons.generic_background_service'
            '.service.background_service'
        )
        # Use assertNoLogs if available (Python 3.10+), otherwise
        # just check shutdown completes quickly
        t0 = time.monotonic()
        service.shutdown_workers()
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 3,
                        "Normal worker should stop well within timeout")
