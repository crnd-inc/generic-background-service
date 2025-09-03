import abc
import threading
import logging
import collections

import psycopg2

import odoo
from .background_service_worker import AbstractBackgroundServiceWorker
from .background_service_registry import BackgroundServiceRegistry

_logger = logging.getLogger(__name__)

# Simple named tuple to store result of database probe.
# It is needed to provide message that describes what is wrong with database
# to be able to log in different layers of application.
DatabaseProbe = collections.namedtuple(
    'DatabaseProbe', ['dbname', 'state', 'message'])

# Default beat timeout (in seconds). Could be float or int
DEFAULT_BEAT_TIMEOUT = 3


# TODO: possibly use __init_subclass__ instead of separate metaclass
class BackgroundServiceMeta(abc.ABCMeta):
    """ Metaclass for services, that user to automatically register
        all subclasses of BackgroundService in service registry
    """

    def __init__(cls, name, bases, attrs):
        super().__init__(name, bases, attrs)
        if cls._name is not None:
            BackgroundServiceRegistry.register_service(cls._name, cls)


class BackgroundService(abc.ABC, metaclass=BackgroundServiceMeta):
    """ Background service will spawn 1 background worker of specified class
        for each active database.
    """

    # Name to be used to register service. If not set, then service
    # will not be registered
    _name = None

    # Name of module, that should be installed in db to make the service work.
    _require_module = None

    # TODO: signal to stop workers before module install/update/uninstall
    def __init__(self):
        # Dict with workers assigned for each database
        # Each worker is a thread
        self._workers = {}

        # Interval to check if database/worker alive
        self._beat_timeout = DEFAULT_BEAT_TIMEOUT

        self._service_event_stop = threading.Event()
        self._service_event_wakeup = threading.Event()

    @property
    def name(self):
        """ Name of service
        """
        return self._name

    def initialize(self):
        """ Could be overloaded in subclasses
            to add some service initialization work.
        """

    @abc.abstractmethod
    def get_worker_class(self) -> AbstractBackgroundServiceWorker:
        """ Returns class to be used to create new service worker
            for this service.
            This method must be implemented by subclass.
        """
        raise NotImplementedError

    def get_worker_params(self):
        """ This method could be used in subclasses to provide
            additional parameters for workers.
        """
        return {}

    def _db_list(self):
        """ Get list of databases for this odoo server.
        """
        if odoo.tools.config['db_name']:
            return odoo.tools.config['db_name'].split(',')
        try:
            db_names = odoo.service.db.list_dbs(True)
        except psycopg2.OperationalError:
            _logger.warning("Cannot obtain list of databases to probe. Possibly postgres is down. Stopping workers.")
            return []
        return db_names

    def _check_is_db_active_cr(self, cr, dbname: str) -> DatabaseProbe:
        cr.execute("""
            SELECT EXISTS(
                SELECT 1
                FROM ir_module_module
                WHERE state LIKE 'to %'
            );
        """)
        if cr.fetchone()[0] is True:
            return DatabaseProbe(
                dbname, False, 'module install/update in progress')

        # Check if required module is installed
        if self._require_module:
            cr.execute("""
                SELECT EXISTS(
                    SELECT 1
                    FROM ir_module_module
                    WHERE state = 'installed'
                      AND name = %(module_name)s
                );
            """, {
                'module_name': self._require_module,
            })
            if cr.fetchone()[0] is not True:
                return DatabaseProbe(
                    dbname, False, 'required module %s not installed' % self._require_module)

        return DatabaseProbe(dbname, True, 'ok')

    def _check_is_db_active(self, dbname: str) -> DatabaseProbe:
        """ Check if database is active. For each active database we have to
            spawn worker.
        """
        try:
            db = odoo.sql_db.db_connect(dbname)
            with db.cursor() as cr:
                check_result = self._check_is_db_active_cr(cr, dbname)
        except psycopg2.OperationalError:
            # If we cannot execute sql statements to check database,
            # then this database is not active, so return False silently
            return DatabaseProbe(
                dbname, False, 'database is not active')
        except psycopg2.errors.UndefinedTable:
            # If some of tables in SQL checks above not present in database,
            # then we have to skip such database.
            # This could happen when new database created
            return DatabaseProbe(
                dbname, False, 'database seems to be not odoo database')

        return check_result

    def _probe_databases(self):
        """ Probe databases, and return list of DatabaseProbe results

            :return list[DatabaseProbe]: database probe results
        """
        return [self._check_is_db_active(db) for db in self._db_list()]

    def spawn_worker(self, dbname: str):
        """ Spawn new worker for specified database

            :param str dbname: Name of database to spawn worker thread for
        """
        params = self.get_worker_params()
        worker_cls = self.get_worker_class()
        worker = worker_cls(self._name, dbname, params)
        self._workers[dbname] = worker
        worker.start()

    def spawn_workers(self):
        """ Spawn workers for active databases
        """
        for dbprobe in self._probe_databases():
            if dbprobe.state and dbprobe.dbname not in self._workers:
                self.spawn_worker(dbprobe.dbname)
            elif not dbprobe.state and dbprobe.dbname not in self._workers:
                _logger.warning(
                    "Database %s skipped for service %s because %s",
                    dbprobe.dbname, self.name, dbprobe.message)

    def stop_worker(self, dbname: str):
        """ Stop worker for specified database

            :param str dbname: name of database to stop worker for
        """
        worker = self._workers.get(dbname)
        if not worker:
            return
        if worker.is_alive() and not worker.worker_is_stopped():
            worker.worker_stop()

    def stop_workers(self):
        """ Stop workers for inactive databases"""
        for dbprobe in self._probe_databases():
            if dbprobe.dbname not in self._workers:
                # Ignore databases that has no workers started
                continue
            if not dbprobe.state:
                # Stop worker if database is not active
                #
                # Note, that here we just send signal to worker to stop it,
                # and expect, that it will shutdown itself.
                #
                # TODO: may be we have to add some additional check if worker
                # was shutdown, and enforce killing it.
                self.stop_worker(dbprobe.dbname)
                _logger.warning(
                    "Stopping worker for service %s for db %s because %s",
                    self.name, dbprobe.dbname, dbprobe.message)

    def clean_workers(self):
        """ Clean dead workers from worker registry
        """
        stopped_dbs = []
        for dbname, worker in self._workers.items():
            if not worker.is_alive():
                stopped_dbs += [dbname]

        for dbname in stopped_dbs:
            del self._workers[dbname]

    def shutdown_workers(self):
        """ Shutdown workers, and wait while they were stopped
        """
        wait_workers = []
        for dbname, worker in self._workers.items():
            self.stop_worker(dbname)
            wait_workers += [worker]
        for worker in wait_workers:
            worker.join()

    def sleep(self):
        """ Make master thread sleep for beat timeout or
            or wakeup event
        """
        self._service_event_wakeup.wait(self._beat_timeout)

    def wakeup(self):
        """ Wakeup services's master thread
        """
        self._service_event_wakeup.set()

    def _run(self):
        while not self._service_event_stop.is_set():
            self.sleep()
            if self._service_event_stop.is_set():
                # Stop event received, so stop the service fast
                break
            self.stop_workers()
            self.clean_workers()
            self.spawn_workers()

    def run(self):
        """ Run the service
        """
        self.initialize()

        _logger.info("Starting background service %s...", self.name)
        try:
            self._run()
        except KeyboardInterrupt:
            _logger.info(
                "Shutting down service %s due to KeyboardInterrupt...",
                self.name)
        except Exception:
            _logger.error("Unrecoverable error. Shutting down background service %s", self.name, exc_info=True)
            raise
        finally:
            _logger.info("Shutting down workers for service %s...", self.name)
            self.shutdown_workers()
            _logger.info("Service %s stopped.", self.name)

    def stop(self):
        """ Stop the service
        """
        _logger.info("Sending 'stop' signal to service %s...", self.name)
        self._service_event_stop.set()
        self._service_event_wakeup.set()
