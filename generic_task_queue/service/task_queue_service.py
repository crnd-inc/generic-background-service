import configparser
import logging

from odoo.addons.generic_background_service import BackgroundService

from .task_queue_worker import TaskQueueWorker

_logger = logging.getLogger(__name__)


def _get_task_queue_config():
    """Read the ``[generic_task_queue]`` section from ``odoo.conf``.

    Example ``odoo.conf`` section::

        [generic_task_queue]
        # Parallel jobs for the default service (default: 1).
        generic_task_queue_service_max_parallel_jobs = 4
        # Parallel jobs for a custom service.
        my_heavy_task_service_max_parallel_jobs = 2

    Keys follow the pattern ``{service_name}_max_parallel_jobs``
    with dots in the service name replaced by underscores.

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
    if parser.has_section('generic_task_queue'):
        return dict(parser.items('generic_task_queue'))
    return {}


class TaskQueueService(BackgroundService):
    """ Background service that processes tasks from the queue.

        Subclasses can override _task_types and _channels to
        create specialized workers that only handle specific
        task types and channels.

        Example::

            class HeavyTaskService(TaskQueueService):
                _name = 'my.heavy.task.service'
                _require_module = 'my_module'
                _task_types = ['task.type.convert.video']
                _channels = ['heavy']
                _max_parallel_jobs = 2

        ``_max_parallel_jobs`` can also be overridden per deployment via
        the ``[generic_task_queue]`` section in ``odoo.conf``::

            [generic_task_queue]
            my_heavy_task_service_max_parallel_jobs = 4

        (Replace dots in the service name with underscores.)
    """
    _name = 'generic.task.queue.service'
    _require_module = 'generic_task_queue'

    # Service manifest — subclasses override these
    _task_types = []        # Empty = all registered types
    _channels = ['default']
    _max_parallel_jobs = 1

    # Stuck task handling
    # -------------------
    # _die_on_stuck_timeout: seconds the service must remain stuck before
    #   _on_service_stuck() is called. Read by BackgroundService._check_stuck()
    #   via MRO. Self-healing triggers when every parallel slot is occupied
    #   by a timed-out thread (is_stuck() == True) continuously for this
    #   duration. In worker mode (prefork) the service stops → process dies
    #   → Odoo respawns → _cleanup_orphaned_tasks() recovers stuck tasks.
    #   In threaded mode the service logs only — natural self-healing when
    #   stuck threads eventually complete.
    # _default_task_timeout: fallback timeout (seconds) applied when a
    #   task has no timeout set and the task type has no default_timeout.
    #   Acts as a safety net — tasks that genuinely need more time should
    #   set default_timeout=0 on their task type to opt out explicitly.
    #   0 = no timeout (not recommended for production).
    _die_on_stuck_timeout = 300
    _default_task_timeout = 3600  # 1 hour safety net

    def _get_channels(self):
        return self._channels

    def _on_service_stuck(self):
        _logger.error(
            "Service %s has been stuck for %.0f seconds, "
            "requesting hard reload.",
            self.name, self._die_on_stuck_timeout)
        self.request_hard_reload()

    def _get_max_parallel_jobs(self):
        """ Return max_parallel_jobs, with ``odoo.conf`` override.

            Reads from the ``[generic_task_queue]`` section.
            Key: ``{service_name}_max_parallel_jobs``
            (dots replaced with underscores).
        """
        key = '%s_max_parallel_jobs' % self._name.replace('.', '_')
        raw = _get_task_queue_config().get(key)
        if raw is not None:
            try:
                return int(raw)
            except (ValueError, TypeError):
                _logger.warning(
                    "Invalid value for [generic_task_queue].%s: %r"
                    " — falling back to _max_parallel_jobs=%d",
                    key, raw, self._max_parallel_jobs)
        return self._max_parallel_jobs

    def get_worker_class(self):
        return TaskQueueWorker

    def get_worker_params(self):
        return {
            'service_name': self._name,
            'task_types': self._task_types,  # empty = all types
            'channels': self._get_channels(),
            'max_parallel_jobs': self._get_max_parallel_jobs(),
            'default_task_timeout': self._default_task_timeout,
        }
