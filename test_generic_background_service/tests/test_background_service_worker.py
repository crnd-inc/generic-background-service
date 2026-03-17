import logging
import threading

from odoo.tests.common import TransactionCase

from odoo.addons.generic_background_service.service.background_service_worker import (  # noqa: E501
    AbstractBackgroundServiceWorker,
)


class FailingOnInitWorker(AbstractBackgroundServiceWorker):
    """Worker whose on_init() raises an exception."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_shutdown_called = threading.Event()
        self.on_error_called = threading.Event()
        self.run_service_called = threading.Event()

    def on_init(self):
        raise RuntimeError("on_init failed")

    def on_error(self, exc):
        self.on_error_called.set()

    def on_shutdown(self):
        self.on_shutdown_called.set()

    def run_service(self):
        self.run_service_called.set()

    def get_sleep_timeout(self):
        return 0.1


class TestWorkerOnInitError(TransactionCase):
    """Test that on_init() exceptions are handled gracefully.

    Bug: on_init() is called outside the try/except block in run(),
    so if it raises, the worker thread dies silently - no on_error(),
    no on_shutdown(), and the service master won't know until next beat.
    """

    def _run_failing_worker(self):
        """Helper to run a FailingOnInitWorker with suppressed
        expected ERROR logs from the worker thread."""
        worker = FailingOnInitWorker('test.service', 'testdb', {})
        worker_logger = logging.getLogger(
            'odoo.addons.generic_background_service'
            '.service.background_service_worker')
        prev_level = worker_logger.level
        worker_logger.setLevel(logging.CRITICAL)
        try:
            worker.start()
            worker.join(timeout=3.0)
        finally:
            worker_logger.setLevel(prev_level)
        return worker

    def test_on_init_error_calls_on_shutdown(self):
        worker = self._run_failing_worker()
        self.assertFalse(
            worker.is_alive(),
            "Worker should have exited")
        self.assertTrue(
            worker.on_shutdown_called.is_set(),
            "on_shutdown() must be called even when on_init() fails")

    def test_on_init_error_prevents_run_service(self):
        worker = self._run_failing_worker()
        self.assertFalse(
            worker.run_service_called.is_set(),
            "run_service() should not be called when on_init() fails")
