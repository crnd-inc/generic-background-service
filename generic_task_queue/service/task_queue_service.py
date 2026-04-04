import logging

from odoo.addons.generic_background_service import BackgroundService

from .task_queue_worker import TaskQueueWorker

_logger = logging.getLogger(__name__)


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

    def _on_service_stuck(self):
        _logger.error(
            "Service %s has been stuck for %.0f seconds, "
            "requesting hard reload.",
            self.name, self._die_on_stuck_timeout)
        self.request_hard_reload()

    def get_worker_class(self):
        return TaskQueueWorker

    def get_worker_params(self):
        return {
            'task_types': self._task_types,  # empty = all types
            'channels': self._channels,
            'max_parallel_jobs': self._max_parallel_jobs,
            'default_task_timeout': self._default_task_timeout,
        }
