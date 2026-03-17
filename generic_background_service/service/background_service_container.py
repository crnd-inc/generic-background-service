import logging

import threading
from odoo.service import server

_logger = logging.getLogger(__name__)


# TODO: Possibly, in case of running in worker mode, we have to run separate
#       separate worker for each service.
#       Or possibly few workers for some service
def wrap_service_as_thread(service_cls):
    """ This function wraps service in container suitable for running
        it in threaded mode.

        :return: new class (that is subclass of Thread) that could be used
            to start wrapped service in separate thread.
    """

    class ServiceContainerThread(threading.Thread):
        """ Service container to run service in threaded mode
        """

        def __init__(self):
            self.service = service_cls()
            super().__init__(
                name='BGService-%s' % self.service.name)

        def run(self):
            self.service.run()

        def stop(self):
            self.service.stop()

    return ServiceContainerThread


def wrap_service_as_worker(service_cls):
    """ This function wraps service in container suitable for running
        it in worker mode.

        :return: new class (that is subclass of server.Worker)
            that could be used to start wrapped service in separate worker
            process.
    """

    class ServiceContainerWorker(server.Worker):
        """ Service container to run service in worker mode
        """

        def __init__(self, multi):
            super().__init__(multi)
            self.watchdog_timeout = None
            self.service = service_cls()

        def sleep(self):
            pass

        def signal_handler(self, sig, frame):
            _logger.debug(
                "Service container (%s [%s]) received signal %s",
                self.service.name, self.pid, sig)
            res = super().signal_handler(sig, frame)
            self.service.stop()
            return res

        def process_work(self):
            _logger.debug(
                "Service container (%s [%s]) starting up",
                self.service.name, self.pid)
            self.service.run()

    return ServiceContainerWorker
