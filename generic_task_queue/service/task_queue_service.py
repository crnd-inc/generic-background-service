from odoo.addons.generic_background_service import BackgroundService

from .task_queue_worker import TaskQueueWorker


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
    # _max_stuck_jobs: number of simultaneously stuck threads that
    #   triggers the die-on-stuck countdown. 0 = feature disabled.
    # _die_on_stuck_timeout: seconds the stuck count must remain at or
    #   above _max_stuck_jobs before the worker stops itself.
    #   In worker mode (prefork) the process dies and Odoo respawns it.
    #   In threaded mode the worker thread dies — manual restart needed.
    # _default_task_timeout: fallback timeout (seconds) applied when a
    #   task has no timeout set and the task type has no default_timeout.
    #   Acts as a safety net — tasks that genuinely need more time should
    #   set default_timeout=0 on their task type to opt out explicitly.
    #   0 = no timeout (not recommended for production).
    _max_stuck_jobs = 0
    _die_on_stuck_timeout = 300
    _default_task_timeout = 3600  # 1 hour safety net

    def get_worker_class(self):
        return TaskQueueWorker

    def get_worker_params(self):
        return {
            'task_types': self._task_types,  # empty = all types
            'channels': self._channels,
            'max_parallel_jobs': self._max_parallel_jobs,
            'max_stuck_jobs': self._max_stuck_jobs,
            'die_on_stuck_timeout': self._die_on_stuck_timeout,
            'default_task_timeout': self._default_task_timeout,
        }
