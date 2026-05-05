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
        # check_signaling() detects registry rebuild or ormcache invalidation
        # signals written by other Odoo processes (module installs, writes that
        # bump base_cache_signaling, etc.) and returns an up-to-date registry.
        # Without this call the worker would serve stale ormcache data for its
        # entire lifetime.  check_signaling() may return a *new* registry
        # object if a rebuild was triggered, so we store the result.
        registry = self.worker_registry.check_signaling()
        self._worker_registry = registry
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
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
        """ Check if stop signal received for this worker"""
        return self._worker_event_stop.is_set()

    def is_stuck(self) -> bool:
        """Return True if this worker cannot make progress.

        Default: always False. Override in concrete workers to expose
        domain-specific stuck detection. Called by BackgroundService to
        determine whether the service as a whole is stuck.
        """
        return False

    def on_init(self):
        """ This method will be called on worker initialization.
            It is safe to access database here to pull worker configuration
            if needed.
        """

    def on_error(self, exc: Exception):
        """ This method will be automatically called on error captured during
            execution of 'run_service' method.
            Could be overridden by subclasses

            :param Exception exc: contains exception catched
        """

    def on_shutdown(self):
        """ This method will be called on shutdown,
            after run_service method is completed and restart is not planned.
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
                If zero - no sleep between service runs (default behavior)
        """
        return 0.0

    def sleep(self, timeout=None):
        """ Sleep until wakeup event received
        """
        if self.get_sleep_timeout() > 0:
            # Wait poll interval timeout or shutdown event
            self._worker_event_wakeup.wait(
                self.get_sleep_timeout() if timeout is None else timeout
            )
            # Clear wakeup event, to allow worker to sleep again on next
            # call to this method
            self._worker_event_wakeup.clear()

    def wakeup(self):
        """ Wakeup this thread.
        """
        self._worker_event_wakeup.set()

    def _run(self):
        """ Main worker loop.
            Do jobs unless stop signal requested.
        """
        while not self._worker_event_stop.is_set():

            try:
                self.run_service()
            except Exception as exc:
                _logger.error(
                    "Error caught during running service worker %s for db %s",
                    self.worker_name, self._worker_dbname, exc_info=True)

                # Run error handler that could be overridden by subclass.
                # By default error is not propagated, just the run_service
                # method restarted (after sleep).
                # If on_error() raises, it is treated as unrecoverable:
                # the worker exits and on_shutdown() is still called
                # (guaranteed by the try/finally in run()).
                self.on_error(exc)

            # Sleep until wakeup event received
            self.sleep()

    def run(self):
        """ Worker lifecycle: on_init → _run loop → on_shutdown.

            on_shutdown() is guaranteed to run via try/finally,
            even if on_init(), run_service(), or on_error() raises.
        """
        _logger.info(
            "Starting service worker %s for '%s' db",
            self.worker_name, self._worker_dbname)

        try:
            try:
                self.on_init()
            except Exception as exc:
                _logger.error(
                    "Error caught during on_init of service worker "
                    "%s for db %s",
                    self.worker_name, self._worker_dbname, exc_info=True)
                self.on_error(exc)
                return

            self._run()
        finally:
            _logger.info(
                "Shutting down service worker %s for '%s' db",
                self.worker_name, self._worker_dbname)
            self.on_shutdown()
            _logger.info(
                "Stopped service worker %s for '%s' db\n",
                self.worker_name, self._worker_dbname)
