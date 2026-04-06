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

    # If True, a toast notification is sent to the task creator when a
    # task of this type reaches a terminal state (done/failed).
    # Set to True in subclasses for user-facing task types where the
    # user needs to know the outcome.
    # task.type.model.method leaves this False — it's a low-level utility.
    _notify_on_completion = False

    # Delay (in seconds) before each automatic retry attempt.
    # The key is the value of retry_count AFTER the failure
    # (i.e. "how long to wait before the Nth retry").
    # Missing keys → 0 (immediate retry — default behaviour).
    #
    # Dict form — explicit per-attempt delays:
    #   _retry_delays = {1: 10, 3: 60, 5: 300}
    #   # wait 10s before 1st retry, 60s before 3rd, 300s before 5th
    #
    # Exponential backoff — min(2**retry_count, 3600) seconds:
    #   _retry_delays = 'exponential'
    #   # 2s, 4s, 8s, 16s … capped at 1h
    #
    # Manual retries from the UI always execute immediately,
    # regardless of this setting.
    _retry_delays = {}

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

            Note: Return value is stored as JSON in the database.
            Keep results small — store references (IDs, paths)
            rather than large data blobs.
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

    def on_child_done(self, env, parent_task, child_task):
        """ Called each time a child task completes.

            Override for incremental progress tracking
            or partial result collection.

            :param env: Odoo environment
            :param parent_task: the parent task record (in 'waiting')
            :param child_task: the child task that just completed
        """

    def on_all_children_done(self, env, parent_task):
        """ Called when all child tasks have completed.

            Override to aggregate results from children.
            Return value becomes the parent's task_result.

            :param env: Odoo environment
            :param parent_task: the parent task record
            :return: aggregated result (stored as JSON in parent)
        """
