import time
import logging
import threading

from odoo.tests.common import TransactionCase

from odoo.addons.generic_background_service.service.background_service import (
    BackgroundService,
    DatabaseProbe,
)
from odoo.addons.generic_background_service.service.background_service_worker import (  # noqa: E501
    AbstractBackgroundServiceWorker,
)
from odoo.addons.generic_background_service.tests.common import (
    BackgroundServiceTestCase,
)


class DummyWorker(AbstractBackgroundServiceWorker):
    """Minimal worker stub for testing BackgroundService."""

    def run_service(self):
        pass  # No-op: just exists to be spawnable

    def get_sleep_timeout(self):
        return 1.0


class DummyBackgroundService(BackgroundService):
    _name = None  # Avoid auto-registration via metaclass

    def get_worker_class(self):
        return DummyWorker

    def _db_list(self):
        return []

    def _check_is_db_active(self, dbname):
        return DatabaseProbe(dbname, True, 'ok')


class StuckWorker(AbstractBackgroundServiceWorker):
    """Worker that blocks in run_service() ignoring stop signal.

    Use ``_entered`` to synchronize: wait for the worker to actually
    enter run_service() before calling shutdown, to avoid the race
    where worker_stop() fires before the while-loop starts.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._block = threading.Event()
        self._entered = threading.Event()

    def run_service(self):
        self._entered.set()
        self._block.wait()  # Block forever

    def get_sleep_timeout(self):
        return 0.0

    def unblock(self):
        """Allow the worker to exit (for cleanup)."""
        self._block.set()
        self.worker_stop()


# ---------------------------------------------------------------
# Tests: sleep/wakeup
# ---------------------------------------------------------------

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


# ---------------------------------------------------------------
# Tests: shutdown timeout
# ---------------------------------------------------------------

class TestShutdownTimeout(TransactionCase):
    """Test that shutdown_workers() respects _shutdown_timeout
    and does not hang indefinitely on stuck workers.
    """

    def _make_service(self, worker_cls, shutdown_timeout):
        service = DummyBackgroundService()
        service._shutdown_timeout = shutdown_timeout
        service.get_worker_class = lambda: worker_cls
        return service

    def _spawn_stuck_worker(self, service, dbname='testdb'):
        """Spawn a StuckWorker and wait until it enters run_service()."""
        service.spawn_worker(dbname)
        worker = service._workers[dbname]
        # Ensure cleanup even if test assertion fails before
        # the explicit unblock() call
        self.addCleanup(worker.unblock)
        self.addCleanup(worker.join, timeout=2)
        self.assertTrue(
            worker._entered.wait(timeout=5),
            "StuckWorker did not enter run_service() in time")
        return worker

    def test_shutdown_workers_does_not_hang_on_stuck_worker(self):
        """shutdown_workers() should return within _shutdown_timeout
        even if a worker ignores the stop signal."""
        timeout = 1
        service = self._make_service(StuckWorker, timeout)
        worker = self._spawn_stuck_worker(service)

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
        worker = self._spawn_stuck_worker(service)

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
        worker = self._spawn_stuck_worker(service)

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

        t0 = time.monotonic()
        service.shutdown_workers()
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 3,
                        "Normal worker should stop well within timeout")


# ---------------------------------------------------------------
# Tests: spawn / stop / clean workers
# ---------------------------------------------------------------

class TestServiceWorkerManagement(BackgroundServiceTestCase):
    """Test BackgroundService worker lifecycle:
    spawn_workers, stop_workers, clean_workers.
    """

    def test_spawn_workers_creates_workers_for_active_dbs(self):
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1', 'db2'],
            active_dbs=['db1', 'db2'],
        )
        service.spawn_workers()

        self.assertIn('db1', service._workers)
        self.assertIn('db2', service._workers)
        self.assertTrue(service._workers['db1'].is_alive())
        self.assertTrue(service._workers['db2'].is_alive())

        service.shutdown_workers()

    def test_spawn_workers_skips_inactive_dbs(self):
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1', 'db2'],
            active_dbs=['db1'],
        )
        service.spawn_workers()

        self.assertIn('db1', service._workers)
        self.assertNotIn('db2', service._workers)

        service.shutdown_workers()

    def test_spawn_workers_does_not_duplicate(self):
        """Calling spawn_workers twice should not create duplicate workers."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        service.spawn_workers()
        worker1 = service._workers['db1']

        service.spawn_workers()
        worker2 = service._workers['db1']

        self.assertIs(worker1, worker2,
                      "spawn_workers should not replace existing worker")

        service.shutdown_workers()

    def test_stop_workers_stops_inactive_db_workers(self):
        """stop_workers() should signal stop for workers whose DB
        became inactive."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1', 'db2'],
            active_dbs=['db1', 'db2'],
        )
        service.spawn_workers()
        self.assertEqual(len(service._workers), 2)

        # Now db2 becomes inactive
        service.set_active_dbs(['db1'])
        service.set_db_list(['db1', 'db2'])
        service.stop_workers()

        # db2 worker should have received stop signal
        worker_db2 = service._workers['db2']
        self.assertTrue(worker_db2.worker_is_stopped())

        # db1 worker should still be running
        worker_db1 = service._workers['db1']
        self.assertFalse(worker_db1.worker_is_stopped())

        service.shutdown_workers()

    def test_stop_workers_when_db_disappears(self):
        """stop_workers() should stop workers for databases that
        disappeared from db_list entirely."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1', 'db2'],
        )
        service.spawn_workers()

        # db2 disappears from the list
        service.set_db_list(['db1'])
        service.set_active_dbs(['db1'])
        service.stop_workers()

        worker_db2 = service._workers['db2']
        self.assertTrue(worker_db2.worker_is_stopped())

        service.shutdown_workers()

    def test_stop_worker_nonexistent_db_is_noop(self):
        """stop_worker() for a database with no worker should be a
        safe no-op."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        # No workers spawned — should not raise
        service.stop_worker('nonexistent')

    def test_stop_worker_already_stopped_is_safe(self):
        """Calling stop_worker() on an already-stopped worker
        should be idempotent."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        service.spawn_workers()
        worker = service._workers['db1']

        # Stop once
        service.stop_worker('db1')
        self.assertTrue(worker.worker_is_stopped())

        # Stop again — should not raise
        service.stop_worker('db1')

        service.shutdown_workers()

    def test_clean_workers_removes_dead_workers(self):
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        service.spawn_workers()
        worker = service._workers['db1']

        # Stop and wait for the worker to die
        worker.worker_stop()
        worker.wakeup()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

        # clean_workers should remove it
        service.clean_workers()
        self.assertNotIn('db1', service._workers)

    def test_clean_workers_keeps_alive_workers(self):
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        service.spawn_workers()

        # Worker is still alive
        service.clean_workers()
        self.assertIn('db1', service._workers)

        service.shutdown_workers()

    def test_spawn_workers_one_bad_db_does_not_block_others(self):
        """If spawn_worker() fails for one database, workers for
        other active databases should still be spawned.

        Bug: spawn_workers() iterates _probe_databases() and calls
        spawn_worker() without try/except. If one spawn_worker()
        raises (e.g. broken worker constructor), the loop aborts
        and remaining databases never get workers.
        """

        # pylint: disable=missing-return
        class BadConstructorWorker(AbstractBackgroundServiceWorker):
            """Worker whose constructor fails for a specific DB."""
            def __init__(self, service_name, dbname, params):
                if dbname == 'bad_db':
                    raise RuntimeError("constructor fails for bad_db")
                super().__init__(service_name, dbname, params)

            def run_service(self):
                pass

            def get_sleep_timeout(self):
                return 1.0

        service = self.create_service(
            worker_cls=BadConstructorWorker,
            db_list=['bad_db', 'good_db'],
            active_dbs=['bad_db', 'good_db'],
        )

        logger_name = (
            'odoo.addons.generic_background_service'
            '.service.background_service'
        )
        svc_logger = logging.getLogger(logger_name)
        prev_level = svc_logger.level
        svc_logger.setLevel(logging.CRITICAL)
        try:
            service.spawn_workers()
        finally:
            svc_logger.setLevel(prev_level)

        # good_db should still have a worker despite bad_db failure
        self.assertIn('good_db', service._workers)
        self.assertTrue(service._workers['good_db'].is_alive())

        service.shutdown_workers()


# ---------------------------------------------------------------
# Tests: service run/stop lifecycle
# ---------------------------------------------------------------

class TestServiceLifecycle(BackgroundServiceTestCase):
    """Test BackgroundService.run() / stop() lifecycle."""

    def test_run_and_stop(self):
        """Service.run() in a thread should exit cleanly after stop()."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        service._beat_timeout = 0.1
        service._shutdown_timeout = 3

        t = threading.Thread(target=service.run)
        t.start()

        # Wait for workers to spawn (proves service is running)
        self._wait_for_workers(service, count=1)
        self.assertTrue(t.is_alive())

        service.stop()
        t.join(timeout=10)
        self.assertFalse(t.is_alive(),
                         "Service should have stopped")

    def test_run_spawns_workers_on_beat(self):
        """Service should spawn workers for active databases
        during its beat loop."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        service._beat_timeout = 0.1
        service._shutdown_timeout = 3

        t = threading.Thread(target=service.run)
        t.start()

        self._wait_for_workers(service, count=1)

        # Workers should have been spawned
        self.assertIn('db1', service._workers)

        service.stop()
        t.join(timeout=10)

    def test_run_shutdown_workers_called_on_stop(self):
        """shutdown_workers() should be called in the finally block
        when service stops."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        service._beat_timeout = 0.1
        service._shutdown_timeout = 3

        t = threading.Thread(target=service.run)
        t.start()

        self._wait_for_workers(service, count=1)

        # Verify worker is running
        self.assertIn('db1', service._workers)
        worker = service._workers['db1']

        service.stop()
        t.join(timeout=10)

        # After service stops, workers should have been shut down
        self.assertFalse(worker.is_alive())

    def test_run_shutdown_workers_on_exception(self):
        """shutdown_workers should be called even if _run raises."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        service._shutdown_timeout = 3

        shutdown_called = threading.Event()
        original_shutdown = service.shutdown_workers

        def tracked_shutdown():
            original_shutdown()
            shutdown_called.set()

        service.shutdown_workers = tracked_shutdown

        # Make _run() raise after the first beat
        def failing_run():
            raise RuntimeError("test error")

        service._run = failing_run

        # Suppress expected ERROR log from service.run() to prevent
        # odood from treating it as a test failure
        logger_name = (
            'odoo.addons.generic_background_service'
            '.service.background_service'
        )
        svc_logger = logging.getLogger(logger_name)
        prev_level = svc_logger.level
        svc_logger.setLevel(logging.CRITICAL)
        try:
            t = threading.Thread(target=service.run)
            t.start()
            t.join(timeout=5)
        finally:
            svc_logger.setLevel(prev_level)

        self.assertTrue(shutdown_called.is_set(),
                        "shutdown_workers must be called on exception")


# ---------------------------------------------------------------
# Tests: database probing with real DB
# ---------------------------------------------------------------

class TestDatabaseProbing(TransactionCase):
    """Test _check_is_db_active_cr() against the real test database.

    Note: During test execution, some modules may be in
    'to install'/'to upgrade' state. We temporarily fix module
    states within the test savepoint for clean testing.
    """

    def _make_service(self, require_module=None):
        service = DummyBackgroundService()
        service._require_module = require_module
        return service

    def _ensure_modules_installed(self):
        """Temporarily set all 'to install'/'to upgrade' modules
        to 'installed' state within the test savepoint, so that
        _check_is_db_active_cr sees a clean state."""
        self.env.cr.execute("""
            UPDATE ir_module_module
            SET state = 'installed'
            WHERE state LIKE 'to %%'
        """)

    def test_db_active_no_module_requirement(self):
        """Database with no pending module operations should
        be active."""
        self._ensure_modules_installed()
        service = self._make_service()
        result = service._check_is_db_active_cr(
            self.env.cr, self.env.cr.dbname)
        self.assertTrue(result.state)
        self.assertEqual(result.message, 'ok')

    def test_db_active_with_installed_module(self):
        """Database should be active when required module
        is installed."""
        self._ensure_modules_installed()
        service = self._make_service(
            require_module='generic_background_service')
        result = service._check_is_db_active_cr(
            self.env.cr, self.env.cr.dbname)
        self.assertTrue(result.state)

    def test_db_inactive_missing_required_module(self):
        """Database should be inactive when required module
        is not installed."""
        self._ensure_modules_installed()
        service = self._make_service(
            require_module='nonexistent_module_xyz')
        result = service._check_is_db_active_cr(
            self.env.cr, self.env.cr.dbname)
        self.assertFalse(result.state)
        self.assertIn('not installed', result.message)

    def test_db_inactive_module_install_in_progress(self):
        """Database should be inactive when modules are being
        installed/upgraded (state LIKE 'to %')."""
        # Force a module into 'to install' state
        self.env.cr.execute("""
            UPDATE ir_module_module
            SET state = 'to install'
            WHERE name = 'base'
        """)
        service = self._make_service()
        result = service._check_is_db_active_cr(
            self.env.cr, self.env.cr.dbname)
        self.assertFalse(result.state)
        self.assertIn('install/update in progress', result.message)


# ---------------------------------------------------------------
# Tests: worker class validation
# ---------------------------------------------------------------

class TestWorkerClassValidation(BackgroundServiceTestCase):
    """Test that spawn_worker() rejects invalid worker classes."""

    def test_spawn_worker_rejects_raw_thread(self):
        """spawn_worker() should raise TypeError if worker class
        doesn't inherit AbstractBackgroundServiceWorker."""
        service = self.create_service(
            worker_cls=threading.Thread,
            db_list=['testdb'],
        )
        with self.assertRaises(TypeError):
            service.spawn_worker('testdb')

    def test_spawn_worker_rejects_non_class(self):
        """spawn_worker() should raise TypeError for non-class values."""
        service = self.create_service(
            worker_cls=None,
            db_list=['testdb'],
        )
        service._test_worker_cls = "not a class"
        with self.assertRaises(TypeError):
            service.spawn_worker('testdb')

    def test_spawn_worker_accepts_valid_worker(self):
        """spawn_worker() should accept a proper
        AbstractBackgroundServiceWorker subclass."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['testdb'],
        )
        service.spawn_worker('testdb')
        self.assertIn('testdb', service._workers)
        self.assertTrue(service._workers['testdb'].is_alive())
        service.shutdown_workers()


# ---------------------------------------------------------------
# Tests: run_service_beats helper
# ---------------------------------------------------------------

class TestRunServiceBeats(BackgroundServiceTestCase):
    """Test the run_service_beats() test framework helper."""

    def test_service_beats_spawns_and_stops(self):
        """run_service_beats() should run the service for N beats,
        spawn workers, and cleanly shut down."""
        service = self.create_service(
            worker_cls=DummyWorker,
            db_list=['db1'],
        )
        t = self.run_service_beats(service, beats=3)
        # After beats complete, service thread should have exited
        self.assertFalse(t.is_alive())


# ---------------------------------------------------------------
# Tests: worker respawn after crash
# ---------------------------------------------------------------

class EphemeralWorker(AbstractBackgroundServiceWorker):
    """Worker that stops itself after the first run_service() cycle.

    Simulates a worker that crashes/exits, to test the service's
    clean_workers() → spawn_workers() respawn cycle.
    """
    spawned_count = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        EphemeralWorker.spawned_count += 1

    def run_service(self):
        self.worker_stop()

    def get_sleep_timeout(self):
        return 0.0


class TestWorkerRespawnAfterCrash(BackgroundServiceTestCase):
    """Test that the service respawns workers after they die."""

    def setUp(self):
        super().setUp()
        EphemeralWorker.spawned_count = 0

    def test_worker_respawned_after_death(self):
        """When a worker dies, the service should remove it via
        clean_workers() and respawn it via spawn_workers()
        on subsequent beats."""
        service = self.create_service(
            worker_cls=EphemeralWorker,
            db_list=['db1'],
        )
        # Run enough beats for: spawn → die → clean → respawn
        self.run_service_beats(service, beats=8, beat_timeout=0.1)

        # Worker should have been spawned at least twice:
        # once initially, and at least once as a respawn
        self.assertGreaterEqual(
            EphemeralWorker.spawned_count, 2,
            "Worker should have been respawned after dying")


# ---------------------------------------------------------------
# Tests: registry late registration
# ---------------------------------------------------------------

class TestRegistryLateRegistration(TransactionCase):
    """Test that BackgroundServiceRegistry rejects service
    registration after initialization."""

    def test_late_registration_is_ignored(self):
        """Services defined after the registry is initialized
        should be silently ignored (with a warning log)."""
        from odoo.addons.generic_background_service.service.\
            background_service_registry import BackgroundServiceRegistry

        # Force initialization (normally done by BackgroundServiceManager)
        orig_allowed = BackgroundServiceRegistry._registration_allowed
        orig_instance = BackgroundServiceRegistry._registry_instance
        try:
            BackgroundServiceRegistry._registration_allowed = False

            logger_name = (
                'odoo.addons.generic_background_service'
                '.service.background_service_registry'
            )
            with self.assertLogs(logger_name, level='WARNING'):
                BackgroundServiceRegistry.register_service(
                    'late.test.service', type('LateService', (), {}))

            # The late service should NOT be in initialized services
            self.assertNotIn(
                'late.test.service',
                BackgroundServiceRegistry.get_initialized_services())
        finally:
            # Restore original state
            BackgroundServiceRegistry._registration_allowed = orig_allowed
            BackgroundServiceRegistry._registry_instance = orig_instance
            # Clean up any accidental registration
            BackgroundServiceRegistry._registered_services.pop(
                'late.test.service', None)
