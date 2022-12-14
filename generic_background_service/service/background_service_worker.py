import logging
import threading
from contextlib import contextmanager

import odoo

_logger = logging.getLogger(__name__)


class AbstractBackgroundServiceWorker(threading.Thread):
    """ Base class for service workers.
        The service will automatically spawn worker for each database.

        The only abstract method that have to be implemented by subclass
        is `run_service` that have to do all the work.

        The workers are based on polling principle.
        Once per polling period, worker will wakeup and check for jobs to do.
        If there is no jobs found, then just go to sleep.

        By default polling interval is set to zero.
        But subclass can implement `get_sleep_timeout` method to define custom
        timeout.
    """

    @classmethod
    def get_worker_name(cls, service_name, dbname) -> str:
        """ Return name of the worker.
            Implemented as class method with params, because it have
            to be passed to super().__init__() to initialize thread.
        """
        return "BGSWorker-%s-%s-%s" % (
            cls.__name__, service_name, dbname)

    # TODO: possibly use custom Event class (lock) to be able to handle
    #       multiple notifications.
    # TODO: Possibly we have to create more complex thread structure,
    #       to be able to handle events received from postgres.
    #       For example, we can use following thread structure:
    #           - Service Thread - Manages threads per database
    #               - Service Database Overseer - Manages worker threads,
    #                                             schedule tasks for threads
    #                   - Service Database Worker 1
    #                   - Service Database Worker 2
    #                   - Service Database Worker 3
    #
    def __init__(self,
                 service_name: str,
                 dbname: str,
                 params: dict = None):
        super().__init__(name=self.get_worker_name(service_name, dbname))
        self._worker_service_name = service_name
        self._worker_dbname = dbname
        self._worker_params = params
        self._worker_event_stop = threading.Event()
        self._worker_event_wakeup = threading.Event()

        # Store odoo registry attached to this worker
        self._worker_registry = None

    @property
    def worker_registry(self) -> odoo.modules.registry.Registry:
        """ Access registry related to this worker.
            If registry is not initialized yet, then initialize it.
        """
        if self._worker_registry is None:
            self._worker_registry = odoo.modules.registry.Registry.new(
                self._worker_dbname)
        return self._worker_registry

    @contextmanager
    def with_env(self):
        with odoo.api.Environment.manage():
            with self.worker_registry.cursor() as cr:
                env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})

                # TODO: Possibly wrap in some error-handling code
                yield env

    @property
    def worker_name(self) -> str:
        """ Property that just returns name of this worker.
        """
        return self.get_worker_name(
            self._worker_service_name, self._worker_dbname)

    def worker_stop(self):
        """ Send stop signal to this worker
        """
        self._worker_event_stop.set()

        # If worker is sleeping, then wakeup (or prevent sleeping
        # if it is running)
        self._worker_event_wakeup.set()

    def worker_is_stopped(self):
        """ Check if stop singal received for this worker"""
        return self._worker_event_stop.is_set()

    def on_error(self, exc: Exception):
        """ This method will be automatically called on error captured during
            execution of 'run_service' method.
            Could be overridden by subclasses

            :param Exception exc: contains exception catched
        """

    def run_service(self):
        """ Main entrypoint for the service.
            Must be overriden by subclass with actual implementation
            of service.
            In case when error is raise or method is finished, then
            it will be started again immediately,
            unless worker is not scheduled for shutdown.
        """
        raise NotImplementedError

    def get_sleep_timeout(self) -> float:
        """ Could be overridden in subclasses.

            :return float: time to sleep in seconds.
        """
        return 0.0

    def sleep(self):
        """ Sleep until wakeup event received
        """
        if self.get_sleep_timeout() > 0:
            # Wait poll interval timeout or shutdown event
            self._worker_event_wakeup.wait(self.get_sleep_timeout())
            # Clear wakeup event, to allow worker to sleep again on next
            # call to this method
            self._worker_event_wakeup.clear()

    def wakeup(self):
        """ Wakeup this thread.
        """
        self._worker_event_wakeup.set()

    def run(self):
        """ Main worker loop.
            Do jobs unless stop signal requested
        """
        _logger.info(
            "Starting service worker %s for '%s' db",
            self.worker_name, self._worker_dbname)
        while not self._worker_event_stop.is_set():

            try:
                self.run_service()
            except Exception as exc:
                _logger.error(
                    "Error caught during running service worker %s for db %s",
                    self.worker_name, self._worker_dbname, exc_info=True)

                # Run error handler that could be overridden by subclass.
                # By default error is not propagated, just the run_service
                # method restarted (after sleep)
                self.on_error(exc)

            # Sleep until wakeup event recaived
            self.sleep()

        _logger.info(
            "Stopped service worker %s for '%s' db\n",
            self.worker_name, self._worker_dbname)
