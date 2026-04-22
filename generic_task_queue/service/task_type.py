import abc
import collections

from .task_type_registry import TaskTypeRegistry

# Returned by iter_child_results() for each child task.
# - result: task_result JSON (only set when state == 'done', else None)
# - error:  task_error text  (only set when state == 'failed', else None)
# - state:  child's final state string
ChildResult = collections.namedtuple(
    'ChildResult', ['task', 'result', 'error', 'state'])

_TERMINAL_STATES = frozenset({'done', 'failed', 'cancelled'})


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

    # Default retry policy for tasks of this type.
    # 'retriable'     — failed tasks are automatically retried
    # 'non_retriable' — tasks are never retried automatically (default)
    # Can be overridden per-task at enqueue time via create_task().
    _retry_policy = 'non_retriable'

    # Maximum number of automatic retries for tasks of this type.
    # After retry_count reaches this value the task stays failed.
    # Can be overridden per-task at enqueue time via create_task().
    _max_retries = 0

    # When True, at most one task of this type may be in the 'assigned' or
    # 'running' state cluster-wide at any time.  The worker skips claiming
    # a new task of this type while another is already executing.
    # Default True — safe for most task types that must not overlap.
    # Set to False for task types that are explicitly designed to run in
    # parallel (e.g. task.type.model.method, where uniqueness is controlled
    # per-enqueue via unique_key).
    _singleton = True

    # When True, update_progress() on this task automatically propagates
    # an averaged progress value upward to the parent task, then recurses
    # until a task type with _propagate_progress = False (or no parent)
    # is reached.  Set on intermediate task types that form levels in a
    # multi-level hierarchy (e.g. a "unit" task that fans out to chunks).
    # The root task type typically leaves this False.
    _propagate_progress = False

    # When True, the base on_child_done() automatically calls
    # parent_task.update_progress() each time a child completes,
    # computing progress as (terminal children / total children * 100).
    # Terminal means done, failed, or cancelled — progress reflects
    # how much of the batch has been processed, not how much succeeded.
    # Override on_child_done() only if you need custom logic beyond
    # simple progress tracking.
    _track_progress = False

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

            The default implementation fires ``update_progress()`` on the
            parent when ``_track_progress = True``, computing progress as::

                terminal_children / total_children * 100

            "Terminal" means done, failed, or cancelled — so progress
            reaches 100 % when all children have finished regardless of
            their outcome.

            Override to add custom incremental logic (e.g. partial result
            collection). Call ``super().on_child_done(...)`` first if you
            still want automatic progress tracking.

            :param env: Odoo environment
            :param parent_task: the parent task record (state 'waiting')
            :param child_task: the child task that just completed
        """
        if not self._track_progress:
            return
        children = parent_task.child_ids
        total = len(children)
        if not total:
            return
        # Invalidate state cache before reading: on_child_done can be
        # called in the same transaction that changed the child state
        # (e.g. in tests), where different env objects share a cursor
        # but keep separate ORM caches.  Forcing a re-read ensures we
        # see the current DB values regardless of how the env was built.
        children.invalidate_recordset(['state'])
        done = sum(1 for c in children if c.state in _TERMINAL_STATES)
        parent_task.update_progress(int(done * 100 / total))

    def on_all_children_done(self, env, parent_task):
        """ Called when all child tasks have completed.

            Override to aggregate results from children.
            Use ``iter_child_results()`` to iterate over children without
            manually checking states::

                def on_all_children_done(self, env, parent_task):
                    items = []
                    for cr in self.iter_child_results(parent_task):
                        if cr.error:
                            continue  # skip failed children
                        items.extend(cr.result.get('ids', []))
                    return {'total': len(items), 'ids': items}

            Return value becomes the parent's task_result.

            :param env: Odoo environment
            :param parent_task: the parent task record
            :return: aggregated result (stored as JSON in parent)
        """

    def iter_child_results(self, parent_task):
        """ Iterate over results of all child tasks.

            Yields a :class:`ChildResult` named tuple for each child:

            - ``task``   — the child task record
            - ``result`` — ``task_result`` JSON when state is ``done``,
                           else ``None``
            - ``error``  — ``task_error`` text when state is ``failed``,
                           else ``None``
            - ``state``  — the child's final state string

            Typically called from :meth:`on_all_children_done`.

            :param parent_task: the parent task record
            :rtype: iterator of ChildResult
        """
        children = parent_task.child_ids
        # Same cache-coherency concern as in on_child_done: invalidate
        # result fields so we always read current DB values.
        children.invalidate_recordset(['state', 'task_result', 'task_error'])
        for child in children:
            yield ChildResult(
                task=child,
                result=child.task_result if child.state == 'done' else None,
                error=child.task_error if child.state == 'failed' else None,
                state=child.state,
            )
