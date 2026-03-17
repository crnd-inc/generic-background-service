import abc
import logging
import threading
from typing import Dict, Type

from .background_service_container import (
    wrap_service_as_worker,
    wrap_service_as_thread,
)
from .background_service_registry import BackgroundServiceRegistry
_logger = logging.getLogger(__name__)


class AbstractBackgroundServiceManager(abc.ABC):
    """ Abstract class for service manager.
        BackgroundServiceManager is responsible for managing running services
    """

    def __init__(self):
        """ Initialize service registry.
        """
        self._service_registry = BackgroundServiceRegistry()
        self._wrapped_services: Dict[str, Type] = None

    @property
    def service_registry(self):
        """ Return link to service registry
        """
        return self._service_registry

    @property
    def services(self):
        """ Provide mapping service name to service container

            :return: dict of format {'service.name': service_container_cls}
        """
        if self._wrapped_services is None:
            self._wrapped_services = {
                name: self.wrap_service(name)
                for name in self.service_registry.get_initialized_services()
            }
        return self._wrapped_services

    @abc.abstractmethod
    def wrap_service(self, name: str):
        """ Wrap service in correct container
        """


class BackgroundServiceManagerWorkerMode(AbstractBackgroundServiceManager):
    """ This class should be used to manage services when odoo is running in
        worker mode.
    """

    def __init__(self):
        """ Initialize service registry.
        """
        super().__init__()

        # Worker registry for case when running in 'worker' mode
        self._server_worker_registry: Dict[str, Dict] = None

    def wrap_service(self, name: str):
        """ Wrap service defined by name in container specified by run_mode
            param of this service registry
        """
        return wrap_service_as_worker(
            self.service_registry.get_service_class(name))

    @property
    def server_worker_registry(self) -> Dict[str, Dict]:
        """ Get registries for service workers
        """
        if self._server_worker_registry is None:
            self._server_worker_registry = {
                service: {} for service in self.services
            }
        return self._server_worker_registry

    def server_worker_pop(self, pid):
        """ This method will be called by odoo master process if odoo is
            running in worker mode.

            In this case, this method will search for specified process id
            if _server_worker_registry and remove it, if found
        """
        # TODO: It seems that this code could be simplified
        for service in self.server_worker_registry:
            if pid in self.server_worker_registry[service]:
                self.server_worker_registry[service].pop(pid)


class BackgroundServiceManagerThreadedMode(AbstractBackgroundServiceManager):
    """ This class have to be used to manage services when odoo is running
        in threaded mode
    """

    def __init__(self):
        """ Initialize service registry.
        """
        super().__init__()

        # Format service -> thread.
        # In threaded mode only one thread per service allowed
        self._threaded_worker_registry: Dict[str, threading.Thread] = {}

    def wrap_service(self, name: str):
        """ Wrap service defined by name in container specified by run_mode
            param of this service registry
        """
        return wrap_service_as_thread(
            self.service_registry.get_service_class(name))

    def threaded_start_services(self):
        """ Start all registered services in threaded mode
        """
        _logger.info("Starting background services in threaded mode")
        for service_name in self.services:
            service_container_cls = self.services[service_name]
            service = service_container_cls()
            # Register service thread in registry
            self._threaded_worker_registry[service_name] = service
            # Start the service
            service.start()

        _logger.info("All background services started in threaded mode")

    def threaded_stop_services(self):
        """ Stop all running services
        """
        _logger.info("Sending stop signal to background services")
        for service_name in self.services:
            service = self._threaded_worker_registry[service_name]
            service.stop()

    # Timeout (in seconds) for waiting on each service container thread
    # during shutdown. This should be long enough for the service to
    # complete its own internal worker shutdown cycle.
    _service_shutdown_timeout = 60

    def threaded_wait_services(self):
        """ Wait while all running services will be stopped.
            (Used Thread.join method to wait for services)
        """
        _logger.info("Waiting for background services to stop")
        for service_name in self.services:
            service = self._threaded_worker_registry[service_name]
            service.join(self._service_shutdown_timeout)
            if service.is_alive():
                _logger.warning(
                    "Service %s did not stop within %s seconds",
                    service_name, self._service_shutdown_timeout)
        _logger.info("All background services stopped.")
