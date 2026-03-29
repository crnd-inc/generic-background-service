import logging
import time
import threading
from contextlib import contextmanager

from odoo.tests.common import TransactionCase

from ..service.background_service import (
    BackgroundService,
    DatabaseProbe,
)
from ..service.background_service_worker import (
    AbstractBackgroundServiceWorker,
)

_logger = logging.getLogger(__name__)


class CycleCountWorker(AbstractBackgroundServiceWorker):
    """Wrapper that counts run_service() cycles and stops the worker
    after a specified number of cycles.

    This is used by BackgroundServiceTestCase.run_worker_cycles()
    to run a real worker in a thread for a controlled number
    of iterations.
    """

    # Set by the factory before start
    _inner_cls = None
    _max_cycles = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cycle_count = 0
        self._inner = self._inner_cls(*args, **kwargs)
        # Forward registry and env access so inner worker can use DB
        self._inner._worker_registry = self._worker_registry
        self._inner.with_env = self.with_env
        # Copy sleep timeout from inner worker
        self.get_sleep_timeout = self._inner.get_sleep_timeout

    def on_init(self):
        self._inner.on_init()

    def on_shutdown(self):
        self._inner.on_shutdown()

    def on_error(self, exc):
        self._inner.on_error(exc)

    def run_service(self):
        self._inner.run_service()
        self._cycle_count += 1
        if self._cycle_count >= self._max_cycles:
            self.worker_stop()


class TestBackgroundService(BackgroundService):
    """A BackgroundService subclass for testing.

    Does not auto-register (_name = None).
    Allows controlling the list of databases and their active state.
    """
    _name = None

    def __init__(self, worker_cls=None, db_list=None, active_dbs=None):
        super().__init__()
        self._test_worker_cls = worker_cls
        self._test_db_list = db_list or []
        self._test_active_dbs = set(active_dbs or self._test_db_list)

    def get_worker_class(self):
        return self._test_worker_cls

    def set_db_list(self, db_list):
        """Update the list of databases returned by _db_list()."""
        self._test_db_list = db_list

    def set_active_dbs(self, active_dbs):
        """Update which databases are considered active."""
        self._test_active_dbs = set(active_dbs)

    def _db_list(self):
        return list(self._test_db_list)

    def _check_is_db_active(self, dbname):
        if dbname in self._test_active_dbs:
            return DatabaseProbe(dbname, True, 'ok')
        return DatabaseProbe(dbname, False, 'test: db not active')


class BackgroundServiceTestCase(TransactionCase):
    """Base test case for testing background services and workers.

    Provides helpers to:
    - Run a worker synchronously for one run_service() cycle
    - Run a worker in a thread for N cycles
    - Create a test service with controllable DB probing
    - Automatic cleanup of threads on tearDown

    **Important limitations:**

    - ``use_test_env=True`` (default for ``run_worker_once``) patches
      ``with_env()`` to share the test's cursor. The worker sees
      uncommitted test data, but there is no real transaction isolation
      — both test and worker operate on the same savepoint.

    - ``use_test_env=False`` lets the worker open its own cursor.
      The worker will only see data that has been **committed** to the
      database, so test-created records (inside a TransactionCase
      savepoint) will not be visible.

    - ``_create_worker()`` pre-sets ``_worker_registry`` to the test
      registry. This bypasses ``Registry.new()``, avoiding recursive
      module loading during tests, but means the worker does not go
      through the normal registry initialization path.

    Usage for addon developers::

        class TestMyWorker(BackgroundServiceTestCase):

            def test_processes_records(self):
                Record = self.env['my.model']
                record = Record.create({'state': 'pending'})

                self.run_worker_once(MyWorkerClass)

                record.invalidate_recordset()
                self.assertEqual(record.state, 'done')
    """

    def setUp(self):
        super().setUp()
        self._tracked_threads = []

    def tearDown(self):
        # Ensure all tracked threads are stopped and joined
        for worker in self._tracked_threads:
            if worker.is_alive():
                worker.worker_stop()
                worker.wakeup()
                worker.join(timeout=5)
                if worker.is_alive():
                    _logger.warning(
                        "Test worker %s still alive after tearDown",
                        worker.name)
        self._tracked_threads.clear()
        super().tearDown()

    def _wait_for_workers(self, service, count=1, timeout=5):
        """Poll until service has >= count workers, or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(service._workers) >= count:
                return
            threading.Event().wait(0.05)
        raise AssertionError(
            "Expected %d workers within %s seconds, got %d"
            % (count, timeout, len(service._workers)))

    def _get_test_dbname(self):
        """Return the database name for the current test."""
        return self.env.cr.dbname

    def _make_test_with_env(self):
        """Create a with_env() context manager that yields the test's
        own environment (switched to superuser). This allows the
        worker to see uncommitted test data, properly initialized
        models, and ormcache.
        """
        test_env = self.env(su=True)

        @contextmanager
        def test_with_env():
            yield test_env

        return test_with_env

    def _create_worker(self, worker_cls, params=None, dbname=None):
        """Create a worker instance bound to the test database.

        Pre-sets the worker's registry to the current test registry
        to avoid Registry.new() triggering recursive module loading
        during test execution.

        :param worker_cls: Worker class (subclass of
            AbstractBackgroundServiceWorker)
        :param dict params: Optional worker params
        :param str dbname: Database name (defaults to test DB)
        :return: Worker instance (not started)
        """
        dbname = dbname or self._get_test_dbname()
        params = params or {}
        worker = worker_cls('test.service', dbname, params)
        # Pre-set registry to avoid Registry.new() during tests
        worker._worker_registry = self.env.registry
        return worker

    def run_worker_once(self, worker_cls, params=None, dbname=None,
                        use_test_env=True):
        """Run a worker synchronously: on_init() + run_service()
        + on_shutdown().

        No threads involved. This is the simplest and most common
        test pattern: set up DB state, run one cycle, verify results.

        By default, the worker's with_env() is patched to reuse the
        test's cursor, so the worker can see uncommitted test data.
        Set use_test_env=False to let the worker open its own cursor
        (useful when the worker writes via complex mixins that
        require a fully independent environment).

        :param worker_cls: Worker class
        :param dict params: Optional worker params
        :param str dbname: Database name (defaults to test DB)
        :param bool use_test_env: If True, patch with_env to use
            the test's cursor (default). If False, use own cursor.
        :return: The worker instance (for inspecting state)
        """
        worker = self._create_worker(worker_cls, params, dbname)
        if use_test_env:
            # Patch with_env to use the test's environment so the
            # worker sees uncommitted test data
            worker.with_env = self._make_test_with_env()
        worker.on_init()
        try:
            worker.run_service()
        finally:
            worker.on_shutdown()
        return worker

    def run_worker_cycles(self, worker_cls, cycles=1,
                          params=None, dbname=None, timeout=None):
        """Run a worker in a thread for N cycles of run_service(),
        then stop.

        Use this when testing loop behavior, error recovery, or
        sleep/wakeup patterns.

        :param worker_cls: Worker class
        :param int cycles: Number of run_service() cycles to execute
        :param dict params: Optional worker params
        :param str dbname: Database name (defaults to test DB)
        :param float timeout: Max seconds to wait (default: cycles * 10)
        :return: The wrapper worker instance
        """
        if timeout is None:
            timeout = max(cycles * 10, 5)

        dbname = dbname or self._get_test_dbname()
        params = params or {}

        # Create a dynamic subclass that stops after N cycles
        wrapper_cls = type(
            'CycleCount_%s_%d' % (worker_cls.__name__, cycles),
            (CycleCountWorker,),
            {
                '_inner_cls': worker_cls,
                '_max_cycles': cycles,
            },
        )

        worker = wrapper_cls('test.service', dbname, params)
        self._tracked_threads += [worker]
        worker.start()
        worker.join(timeout=timeout)

        if worker.is_alive():
            worker.worker_stop()
            worker.wakeup()
            worker.join(timeout=5)
            raise AssertionError(
                "Worker %s did not complete %d cycles within %s seconds"
                % (worker_cls.__name__, cycles, timeout))

        return worker

    def create_service(self, worker_cls=None, db_list=None,
                       active_dbs=None):
        """Create a TestBackgroundService with controllable DB probing.

        :param worker_cls: Worker class for the service
        :param list db_list: List of database names to return
            from _db_list()
        :param list active_dbs: List of databases considered active
            (defaults to all in db_list)
        :return: TestBackgroundService instance
        """
        return TestBackgroundService(
            worker_cls=worker_cls,
            db_list=db_list or [],
            active_dbs=active_dbs,
        )

    def run_service_beats(self, service, beats=3, beat_timeout=0.1):
        """Run a service for N beat cycles in a thread, then stop.

        :param service: BackgroundService instance
        :param int beats: Number of beat cycles to run
        :param float beat_timeout: Seconds per beat cycle
        :return: The service thread
        """
        service._beat_timeout = beat_timeout
        service._shutdown_timeout = 5

        t = threading.Thread(
            target=service.run,
            name='TestService-%s' % (service.name or 'unnamed'))
        t.start()

        # Let the service run for approximately N beats
        beat_event = threading.Event()
        beat_event.wait(beat_timeout * (beats + 1))

        service.stop()
        t.join(timeout=10)

        if t.is_alive():
            _logger.warning("Test service thread still alive after stop")

        return t
