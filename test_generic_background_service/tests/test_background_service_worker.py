import logging
import threading

from odoo.tests.common import TransactionCase

from odoo.addons.generic_background_service.service.background_service_worker import (  # noqa: E501
    AbstractBackgroundServiceWorker,
)
from odoo.addons.generic_background_service.tests.common import (
    BackgroundServiceTestCase,
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


class CountingWorker(AbstractBackgroundServiceWorker):
    """Worker that counts run_service() calls and records errors."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_count = 0
        self.error_count = 0
        self.init_called = threading.Event()
        self.shutdown_called = threading.Event()
        self._fail_until = 0  # fail first N calls to run_service

    def on_init(self):
        self.init_called.set()

    def on_error(self, exc):
        self.error_count += 1

    def on_shutdown(self):
        self.shutdown_called.set()

    def run_service(self):
        self.run_count += 1
        if self.run_count <= self._fail_until:
            raise RuntimeError("intentional failure #%d" % self.run_count)

    def get_sleep_timeout(self):
        return 0.05


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


class TestWorkerRunLoop(TransactionCase):
    """Test the worker's main run() loop: error recovery,
    sleep/wakeup, stop signal.
    """

    def _run_worker_in_thread(self, worker, timeout=5):
        """Start worker thread and wait for it to finish."""
        worker_logger = logging.getLogger(
            'odoo.addons.generic_background_service'
            '.service.background_service_worker')
        prev_level = worker_logger.level
        worker_logger.setLevel(logging.CRITICAL)
        try:
            worker.start()
            worker.join(timeout=timeout)
        finally:
            worker_logger.setLevel(prev_level)
            if worker.is_alive():
                worker.worker_stop()
                worker.wakeup()
                worker.join(timeout=3)

    def test_run_service_error_recovery(self):
        """When run_service() raises, on_error() is called and
        run_service() is retried on next cycle."""
        worker = CountingWorker('test.service', 'testdb', {})
        worker._fail_until = 2  # fail first 2 calls

        # Stop after a few cycles
        def auto_stop():
            while worker.run_count < 4:
                threading.Event().wait(0.05)
            worker.worker_stop()
            worker.wakeup()

        stopper = threading.Thread(target=auto_stop)
        stopper.start()

        self._run_worker_in_thread(worker, timeout=5)
        stopper.join(timeout=3)

        self.assertGreaterEqual(worker.run_count, 4)
        self.assertEqual(worker.error_count, 2)
        self.assertTrue(worker.shutdown_called.is_set())

    def test_worker_stop_during_sleep(self):
        """Worker should exit promptly when stopped during sleep."""
        worker = CountingWorker('test.service', 'testdb', {})
        # Use a long sleep to verify stop interrupts it
        worker.get_sleep_timeout = lambda: 10.0

        worker.start()
        # Wait for at least one run_service call
        while worker.run_count < 1:
            threading.Event().wait(0.01)

        # Stop the worker while it's sleeping
        import time
        t0 = time.monotonic()
        worker.worker_stop()
        worker.wakeup()
        worker.join(timeout=3)
        elapsed = time.monotonic() - t0

        self.assertFalse(worker.is_alive())
        self.assertLess(elapsed, 2.0,
                        "Worker should stop promptly when woken up")

    def test_worker_lifecycle_hooks_order(self):
        """Verify on_init → run_service → on_shutdown order."""
        worker = CountingWorker('test.service', 'testdb', {})

        def auto_stop():
            while not worker.init_called.is_set():
                threading.Event().wait(0.01)
            while worker.run_count < 1:
                threading.Event().wait(0.01)
            worker.worker_stop()
            worker.wakeup()

        stopper = threading.Thread(target=auto_stop)
        stopper.start()

        self._run_worker_in_thread(worker, timeout=5)
        stopper.join(timeout=3)

        self.assertTrue(worker.init_called.is_set())
        self.assertGreaterEqual(worker.run_count, 1)
        self.assertTrue(worker.shutdown_called.is_set())


# ---------------------------------------------------------------
# Tests: worker with real DB access (using test framework)
# ---------------------------------------------------------------

# ---------------------------------------------------------------
# Tests: run_worker_cycles helper
# ---------------------------------------------------------------

class TestRunWorkerCycles(BackgroundServiceTestCase):
    """Test the run_worker_cycles() test framework helper."""

    def test_run_worker_cycles_executes_n_times(self):
        """run_worker_cycles() should run the inner worker's
        run_service() exactly N times."""
        worker = self.run_worker_cycles(CountingWorker, cycles=3)
        self.assertEqual(worker._inner.run_count, 3)
        self.assertFalse(worker.is_alive())

    def test_run_worker_cycles_calls_lifecycle_hooks(self):
        """run_worker_cycles() should call on_init and on_shutdown
        on the inner worker."""
        worker = self.run_worker_cycles(CountingWorker, cycles=1)
        self.assertTrue(worker._inner.init_called.is_set())
        self.assertTrue(worker._inner.shutdown_called.is_set())


# ---------------------------------------------------------------
# Tests: worker with real DB access (using test framework)
# ---------------------------------------------------------------

class TestWorkerWithEnv(BackgroundServiceTestCase):
    """Test that workers can access the database via with_env().

    Note: Worker's with_env() calls Registry.new() which may
    trigger module loading. To avoid this in tests, we pre-set
    the worker's registry to the existing test registry.
    """

    def test_with_env_provides_working_environment(self):
        """Worker's with_env() should provide a working Odoo
        environment connected to the correct database."""

        class EnvCheckWorker(AbstractBackgroundServiceWorker):
            env_dbname = None
            env_works = False

            def run_service(self):
                with self.with_env() as env:
                    EnvCheckWorker.env_dbname = env.cr.dbname
                    # Verify we can query the database
                    env.cr.execute("SELECT 1")
                    EnvCheckWorker.env_works = True

            def get_sleep_timeout(self):
                return 0.0

        self.run_worker_once(EnvCheckWorker)
        self.assertEqual(EnvCheckWorker.env_dbname,
                         self._get_test_dbname())
        self.assertTrue(EnvCheckWorker.env_works)

    def test_db_access_works_after_error_recovery(self):
        """After an error in run_service(), the worker should still
        be able to access the database on the next cycle."""

        class FailThenSucceedWorker(AbstractBackgroundServiceWorker):
            call_count = 0
            error_count = 0
            db_ok_after_error = False

            def run_service(self):
                FailThenSucceedWorker.call_count += 1
                if FailThenSucceedWorker.call_count == 1:
                    raise RuntimeError("simulated failure")
                # Second call: verify DB still works
                with self.with_env() as env:
                    env.cr.execute("SELECT 1")
                    FailThenSucceedWorker.db_ok_after_error = True
                self.worker_stop()

            def on_error(self, exc):
                FailThenSucceedWorker.error_count += 1

            def get_sleep_timeout(self):
                return 0.0

        worker = self._create_worker(FailThenSucceedWorker)
        worker.with_env = self._make_test_with_env()

        # Suppress expected error log
        worker_logger = logging.getLogger(
            'odoo.addons.generic_background_service'
            '.service.background_service_worker')
        prev_level = worker_logger.level
        worker_logger.setLevel(logging.CRITICAL)
        try:
            worker.start()
            worker.join(timeout=5)
        finally:
            worker_logger.setLevel(prev_level)
            if worker.is_alive():
                worker.worker_stop()
                worker.wakeup()
                worker.join(timeout=3)

        self.assertEqual(FailThenSucceedWorker.call_count, 2)
        self.assertEqual(FailThenSucceedWorker.error_count, 1)
        self.assertTrue(FailThenSucceedWorker.db_ok_after_error,
                        "DB access should work after error recovery")
