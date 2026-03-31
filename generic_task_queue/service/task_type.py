import abc

from .task_type_registry import TaskTypeRegistry


class AbstractTaskType(abc.ABC):
    """ Base class for task types.

        A task type defines how to execute a specific category of work.
        Subclasses must implement execute() and set a unique _name.

        Task types are auto-registered in TaskTypeRegistry via
        __init_subclass__. Set _name = None to prevent registration
        (useful for abstract intermediate classes).

        Example::

            class MyTaskType(AbstractTaskType):
                _name = 'my.module.my.task'

                def execute(self, env, task):
                    record = env['my.model'].browse(
                        task.task_params['record_id'])
                    record.do_work()
                    return {'status': 'ok'}
    """

    # Dotted name for registry. If None, the class is not registered.
    _name = None

    def __init_subclass__(cls, **kwargs):
        result = super().__init_subclass__(**kwargs)
        if cls._name is not None:
            TaskTypeRegistry.register_type(cls._name, cls)
        return result

    @abc.abstractmethod
    def execute(self, env, task):
        """ Execute the task.

            :param env: Odoo environment (with cursor)
            :param task: recordset of generic.task.queue.task (single record)
            :return: result value (stored as JSON in task.task_result)

            For long-running tasks:

            - Call ``task.update_progress(pct)`` periodically
              to report progress (0-100).
            - Check ``task.is_cancelled()`` and return early if True.
            - Create child tasks via
              ``env['generic.task.queue.task'].create(...)``
              to split work into sub-tasks.
        """
        raise NotImplementedError

    def on_success(self, env, task, result):
        """ Called after successful execution.

            Override to add custom post-processing
            (e.g., notify user, trigger next step).

            :param env: Odoo environment
            :param task: task record (state is already 'done')
            :param result: return value from execute()
        """

    def on_failure(self, env, task, exc):
        """ Called after failed execution.

            Override to add custom error handling
            (e.g., log to chatter, send alert).

            :param env: Odoo environment
            :param task: task record (state is already 'failed')
            :param exc: the exception that was raised
        """
