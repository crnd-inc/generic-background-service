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

    def get_worker_class(self):
        return TaskQueueWorker

    def get_worker_params(self):
        return {
            'task_types': self._task_types,  # empty = all types
            'channels': self._channels,
            'max_parallel_jobs': self._max_parallel_jobs,
        }
