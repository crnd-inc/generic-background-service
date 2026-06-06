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
    # 'no_retry'    — never auto-retry (default)
    # 'retry_known' — retry on known-transient errors and explicit RetryTask
    # 'retry_any'   — retry on any exception until max_retries exhausted
    # Can be overridden per-task at enqueue time via create_task().
    _retry_policy = 'no_retry'

    # Maximum number of automatic retries for tasks of this type.
    # After retry_count reaches this value the task stays failed.
    # Can be overridden per-task at enqueue time via create_task().
    _max_retries = 0

    # Service affinity. When set, only the TaskQueueService with this _name
    # may claim tasks of this type. None means any service may claim it.
    _service_name = None

    # Default channel used by create_task() when no channel is passed.
    # Keeps routing self-describing at the task type level.
    _default_channel = 'default'

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

            Keep this hook fast. It runs inside the same transaction as
            execute() and action_done(), holding the task row lock. Slow
            work should be enqueued as a separate child task instead.

            The hook runs inside a savepoint: a DB error rolls back only
            the hook's changes; execute() side-effects and action_done()
            still commit normally.

            :param env: Odoo environment
            :param task: task record (state is still 'running'; action_done
                is called by the worker after this hook returns)
            :param result: return value from execute()
        """

    def on_failure(self, env, task, exc):
        """ Called after failed execution.

            Override to add custom error handling
            (e.g., log to chatter, send alert).

            Keep this hook fast. It runs inside the same transaction as
            action_fail(), holding the task row lock. Slow work should be
            enqueued as a separate task instead.

            The hook runs inside a savepoint: a DB error rolls back only
            the hook's changes; action_fail() still commits normally.

            :param env: Odoo environment
            :param task: task record (state is still 'running'; action_fail
                is called by the worker after this hook returns)
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

            Keep this hook fast and free of operations that could fail.
            It runs inside a savepoint: a DB error rolls back only the
            hook's changes; ``action_done()`` still commits normally.
            For complex aggregation or validation that could fail, enqueue
            a separate child task instead.

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
        # _child_results invalidates the result fields before reading, so we
        # always see current DB values (the same cache-coherency concern as
        # in on_child_done).
        return _child_results(parent_task.child_ids)


def _child_results(children):
    """ Yield a :class:`ChildResult` for each task in ``children``. """
    children.invalidate_recordset(['state', 'task_result', 'task_error'])
    for child in children:
        yield ChildResult(
            task=child,
            result=child.task_result if child.state == 'done' else None,
            error=child.task_error if child.state == 'failed' else None,
            state=child.state,
        )


class MultiPhaseTaskType(AbstractTaskType):
    """ Base class for declarative multi-phase pipelines.

        A multi-phase task runs as a *single root* that fans out one wave of
        child tasks per phase: spawn phase 0's wave → wait for it → spawn
        phase 1's wave under the same root → wait → … → done. One root means
        one progress bar, one completion notification, and one
        ``action_cancel`` that cascades to every live child of any phase.

        Subclasses set :attr:`_phases` (an ordered list of phase-name
        strings) and implement :meth:`plan_phase`, which returns the children
        for a given phase. The framework drives the sequence: it spawns the
        first phase in :meth:`execute` and advances through the rest in
        :meth:`on_all_children_done`. Built on the re-arm primitive in
        ``generic.task.queue.task._check_waiting_parent`` — when the hook
        spawns a new wave the parent stays ``waiting`` instead of completing.

        Example::

            class ExportPipeline(MultiPhaseTaskType):
                _name   = 'my.export.pipeline'
                _phases = ['prepare', 'upload']

                def plan_phase(self, env, task, phase, prev_results):
                    if phase == 'prepare':
                        return ('my.prepare.chunk',
                                [{'record_ids': c} for c in chunks(task)])
                    if phase == 'upload':
                        doc_ids = collect(prev_results)
                        return ('my.upload.chunk',
                                [{'doc_ids': b}
                                 for b in batches(doc_ids)])

        For pipelines that need heterogeneous child types within one wave, or
        per-child reactive spawning, drop down to the raw primitive
        (``task.spawn_children(...)`` + ``action_wait_children()``) instead.
    """

    # Abstract base — not registered in TaskTypeRegistry.
    _name = None

    # Ordered list of phase names. Drives the sequence of child waves.
    _phases = []

    # Multi-phase pipelines report progress across phases by default.
    _track_progress = True

    def plan_phase(self, env, task, phase, prev_results):
        """ Return the children to spawn for ``phase``.

            :param env: Odoo environment
            :param task: the root (pipeline) task record
            :param str phase: the phase name (an entry of :attr:`_phases`)
            :param prev_results: list of :class:`ChildResult` for the wave
                that just finished, or ``None`` for the first phase
            :return: a ``(type_code, params_list)`` tuple — the task type for
                this wave and the list of per-child ``task_params`` dicts.
                Return ``None`` or an empty ``params_list`` to skip the phase
                (no children; advance immediately to the next phase).
        """
        raise NotImplementedError

    def aggregate_result(self, env, task, last_results):
        """ Build the root task's final ``task_result`` after the last phase.

            Defaults to ``None``. Override to summarise the pipeline.

            :param last_results: list of :class:`ChildResult` for the final
                wave (empty when the last phase spawned no children).
            :return: aggregated result (stored as JSON on the root task)
        """
        return None

    def execute(self, env, task):
        """ Spawn the first non-empty phase.

            If no phase produces any children the pipeline is empty:
            ``_start_wave`` returns ``False`` and ``execute`` returns
            normally, so the worker completes the task via the regular
            done path.
        """
        self._start_wave(env, task, 0, None)

    def on_all_children_done(self, env, parent_task):
        """ Advance to the next phase, or finalize the pipeline.

            Spawns the next non-empty phase's wave (keeping the parent in
            ``waiting`` via the re-arm primitive). When no phases remain,
            returns the aggregated result so the parent completes.
        """
        prev_results = list(self.iter_wave_results(parent_task))
        spawned = self._start_wave(
            env, parent_task, parent_task.phase + 1, prev_results)
        if spawned:
            # New wave is live; the parent stays 'waiting'. This return value
            # is ignored — _check_waiting_parent sees the fresh children and
            # does not complete the parent.
            return None
        return self.aggregate_result(env, parent_task, prev_results)

    def _start_wave(self, env, task, phase_index, prev_results):
        """ Spawn the first phase at or after ``phase_index`` that yields
            children. Records the phase index and the wave's child ids on
            the task (merging into ``phase_data`` so any state stashed by
            ``plan_phase`` via ``store_phase_data`` is preserved). Returns
            ``True`` if a wave was spawned, ``False`` if no remaining phase
            produced any children.
        """
        while phase_index < len(self._phases):
            phase = self._phases[phase_index]
            type_code, params_list = self._normalize_plan(
                self.plan_phase(env, task, phase, prev_results))
            if params_list:
                children = task.spawn_children(type_code, params_list)
                phase_data = dict(task.phase_data or {})
                phase_data['child_ids'] = children.ids
                task.write({
                    'phase': phase_index,
                    'phase_data': phase_data,
                })
                if task.state != 'waiting':
                    task.action_wait_children()
                return True
            phase_index += 1
        # Pipeline exhausted without spawning any children.
        task.write({'phase': phase_index})
        return False

    @staticmethod
    def _normalize_plan(plan):
        if not plan:
            return None, []
        type_code, params_list = plan
        return type_code, list(params_list or [])

    def iter_wave_results(self, parent_task):
        """ Like :meth:`iter_child_results`, but limited to the children of
            the current (most recently finished) phase wave.
        """
        ids = (parent_task.phase_data or {}).get('child_ids') or []
        children = parent_task.env['generic.task.queue.task'].browse(ids)
        return _child_results(children)

    def on_child_done(self, env, parent_task, child_task):
        """ Phase-aware progress: each phase contributes an equal slice, so
            progress advances monotonically across phase boundaries instead
            of resetting when a new (larger or smaller) wave is spawned::

                progress = 100 * (phase + done_in_wave / wave_total)
                                / total_phases
        """
        if not self._track_progress:
            return
        total_phases = len(self._phases) or 1
        wave_ids = (parent_task.phase_data or {}).get('child_ids') or []
        wave = parent_task.env['generic.task.queue.task'].browse(wave_ids)
        wave.invalidate_recordset(['state'])
        wave_total = len(wave)
        if wave_total:
            done_in_wave = sum(
                1 for c in wave if c.state in _TERMINAL_STATES)
            wave_fraction = done_in_wave / wave_total
        else:
            wave_fraction = 0
        pct = int(100 * (parent_task.phase + wave_fraction) / total_phases)
        parent_task.update_progress(pct)
