import configparser
import logging
import resource
import threading

from odoo.service import server

# Default CPU time limit for background service workers (seconds).
# Much larger than limit_time_cpu (60s) — background workers are long-running.
DEFAULT_LIMIT_TIME_CPU_BACKGROUND = 3600

_logger = logging.getLogger(__name__)


def _read_background_service_config():
    """Return the raw ``[generic_background_service]`` section from odoo.cfg
    as a plain ``{option: str}`` dict.

    Odoo 19 dropped support for non-``[options]`` config sections
    (``config.misc`` / ``config.get_misc`` were removed), so read the section
    directly from the active config file.
    """
    from odoo.tools import config as odoo_config

    rcfile = odoo_config.get('config')
    if not rcfile:
        return {}

    parser = configparser.RawConfigParser()
    try:
        parser.read([rcfile])
    except (configparser.Error, OSError):
        return {}
    if parser.has_section('generic_background_service'):
        return dict(parser.items('generic_background_service'))
    return {}


def _get_background_service_config():
    """Read config for background workers from [generic_background_service]
    in odoo.cfg.

    Example odoo.cfg section::

        [generic_background_service]
        # CPU time budget per worker process (seconds). Default: 3600.
        limit_time_cpu = 3600
    """
    raw = _read_background_service_config()

    def _int(key, default):
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            _logger.warning(
                "Invalid value for [generic_background_service].%s,"
                " using default %s", key, default)
            return default

    return {
        'limit_time_cpu': _int(
            'limit_time_cpu', DEFAULT_LIMIT_TIME_CPU_BACKGROUND),
    }


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
            self.service = service_cls(execution_mode='threaded')
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
            self.service = service_cls(execution_mode='worker')
            self._limit_time_cpu = (
                _get_background_service_config()['limit_time_cpu'])

        def sleep(self):
            pass

        def _reset_cpu_limit(self):
            """Set RLIMIT_CPU to current_cpu + _limit_time_cpu.

            Called from check_limits() to restore our large per-worker budget
            after super().check_limits() overwrites it with the short
            limit_time_cpu value (default 60s) intended for HTTP workers.
            """
            r = resource.getrusage(resource.RUSAGE_SELF)
            cpu_time = r.ru_utime + r.ru_stime
            soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
            new_soft = int(cpu_time + self._limit_time_cpu)
            if hard != resource.RLIM_INFINITY and new_soft > hard:
                _logger.warning(
                    "Service container (%s [%s]) cannot set CPU limit"
                    " to %ss (hard limit is %ss)",
                    self.pid, self.service.name, new_soft, hard)
                new_soft = hard
            resource.setrlimit(resource.RLIMIT_CPU, (new_soft, hard))
            _logger.debug(
                "Service container (%s [%s]) CPU limit set:"
                " %ss from now (cumulative: %ss)",
                self.pid, self.service.name,
                self._limit_time_cpu, int(cpu_time))

        def check_limits(self):
            was_alive = self.alive
            res = super().check_limits()
            # super() just set RLIMIT_CPU to current_cpu + limit_time_cpu
            # (default 60s).  Restore our larger background-worker budget.
            self._reset_cpu_limit()
            # If super() triggered shutdown (e.g. memory limit), drain
            # the service gracefully instead of hard-killing it.
            if was_alive and not self.alive:
                self.service.stop()
            return res

        def signal_handler(self, sig, frame):
            _logger.debug(
                "Service container (%s [%s]) received signal %s",
                self.service.name, self.pid, sig)
            res = super().signal_handler(sig, frame)
            self.service.stop()
            return res

        def signal_time_expired_handler(self, n, stack):
            # Do NOT raise (base class raises → _runloop catches →
            # sys.exit(1) → hard kill with no drain).
            # Instead set alive=False and stop the service so that
            # process_work() returns cleanly, _runloop exits, and
            # PreforkServer spawns a fresh replacement worker.
            _logger.info(
                "Service container (%s [%s]) CPU time limit (%ss)"
                " reached, initiating graceful restart",
                self.pid, self.service.name, self._limit_time_cpu)
            self.alive = False
            self.service.stop()

        def process_work(self):
            _logger.debug(
                "Service container (%s [%s]) starting up",
                self.service.name, self.pid)
            self.service.run()
            if self.service._hard_reload_requested:
                _logger.info(
                    "Service container (%s [%s]) hard reload: "
                    "exiting process for respawn.",
                    self.pid, self.service.name)
                self.alive = False

    return ServiceContainerWorker
