import logging

from odoo.service import server
from odoo.tools import config

from odoo.addons.generic_mixin.tools.monkey import monkey
from .service.background_service_manager import (
    BackgroundServiceManagerWorkerMode,
    BackgroundServiceManagerThreadedMode,
)


_logger = logging.getLogger(__name__)


if not config["stop_after_init"]:
    # We have to run services only if odoo running as server
    # (without option --stop-after-init)

    # -------------------------
    # Prefork server overrides
    # -------------------------
    @monkey(server.PreforkServer, '__init__')
    def prefork__init__(_server, app):
        res = prefork__init__.__wrapped__(_server, app)

        # Initialize background service manager
        _server._background_service_manager = (
            BackgroundServiceManagerWorkerMode())

        return res

    @monkey(server.PreforkServer, 'process_spawn')
    def prefork_process_spawn(_server):
        prefork_process_spawn.__wrapped__(_server)

        if not hasattr(_server, "_background_service_manager"):
            # this could happen, if module is not mentioned in
            # server_wide_modules. So, in this case do nothing
            return

        # Spawn needed workers for each services
        srv_manager = _server._background_service_manager
        for srv_name, srv_cls in srv_manager.services.items():
            worker_registry = srv_manager.server_worker_registry[srv_name]
            # Currently, only 1 worker per service allowed.
            # TODO: Do we need multiple workers for service?
            #       How to communicate between them?
            #       Possibly, at first we have to provide mechanisms for
            #       communication between workers and services running on
            #       different nodes.
            if len(worker_registry) < 1:
                _server.worker_spawn(srv_cls, worker_registry)

    @monkey(server.PreforkServer, 'worker_pop')
    def prefork_worker_pop(_server, pid):
        prefork_worker_pop.__wrapped__(_server, pid)
        if not hasattr(_server, "_background_service_manager"):
            # this could happen, if module is not mentioned in
            # server_wide_modules. So, in this case do nothing
            return

        _server._background_service_manager.server_worker_pop(pid)
        return

    # -------------------------
    # Threaded server overrides
    # -------------------------
    @monkey(server.ThreadedServer, '__init__')
    def threaded_init(_server, app):
        threaded_init.__wrapped__(_server, app)

        # Here we have to initialize service manager. It will be used
        # to manage threads allocated for running services
        _server._background_service_manager = (
            BackgroundServiceManagerThreadedMode())

    @monkey(server.ThreadedServer, 'start')
    def threaded_start(_server, *args, **kwargs):
        res = threaded_start.__wrapped__(_server, *args, **kwargs)
        if not config["stop_after_init"]:
            _server._background_service_manager.threaded_start_services()
        return res

    @monkey(server.ThreadedServer, 'stop')
    def threaded_stop(_server):
        if getattr(_server, '_background_service_manager', None):
            _server._background_service_manager.threaded_stop_services()
        res = threaded_stop.__wrapped__(_server)
        if getattr(_server, '_background_service_manager', None):
            _server._background_service_manager.threaded_wait_services()
        return res
