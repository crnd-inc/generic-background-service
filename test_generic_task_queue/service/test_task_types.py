from odoo.addons.generic_task_queue import (
    AbstractTaskType, MultiPhaseTaskType, TaskListSpec)


class TestTaskTypeNoOp(AbstractTaskType):
    """Task type that does nothing. For registry tests."""
    _name = 'test.task.type.noop'
    _singleton = False

    def execute(self, env, task):
        return {'status': 'noop'}


class TestTaskTypeEcho(AbstractTaskType):
    """Task type that returns its params back. For testing."""
    _name = 'test.task.type.echo'
    _singleton = False

    def execute(self, env, task):
        return task.task_params


class TestTaskTypeBatchParent(AbstractTaskType):
    """Task type that splits work into child tasks.

    Demonstrates parent/child pattern:
    - Parent receives list of items in params
    - Creates one child per chunk
    - Waits for all children to complete
    - Aggregates results
    """
    _name = 'test.task.type.batch.parent'
    _singleton = False
    _track_progress = True

    _chunk_size = 2

    def execute(self, env, task):
        items = task.task_params.get('items', [])
        Task = env['generic.task.queue.task']

        # Split items into chunks and create child tasks
        chunks = [
            items[i:i + self._chunk_size]
            for i in range(0, len(items), self._chunk_size)
        ]
        Task.create_children(
            task, type_code='test.task.type.batch.child',
            params_list=[{'items': chunk} for chunk in chunks])

        # Transition to waiting
        task.action_wait_children()

    def on_all_children_done(self, env, parent_task):
        all_results = []
        for cr in self.iter_child_results(parent_task):
            if cr.result:
                all_results.extend(cr.result.get('processed', []))
        return {
            'total_processed': len(all_results),
            'items': all_results,
        }


class TestTaskTypePropagatingChild(AbstractTaskType):
    """Child task type that propagates its progress to the parent.

    Used to test the _propagate_progress flag.
    """
    _name = 'test.task.type.propagating.child'
    _singleton = False
    _propagate_progress = True

    def execute(self, env, task):
        return {}


class TestTaskTypeBatchChild(AbstractTaskType):
    """Child task type that processes a chunk of items."""
    _name = 'test.task.type.batch.child'
    _singleton = False

    def execute(self, env, task):
        items = task.task_params.get('items', [])
        # Simulate processing: double each item
        processed = [i * 2 for i in items]
        return {'processed': processed}


class TestTaskTypeSingleton(AbstractTaskType):
    """Singleton task type for concurrency guard tests."""
    _name = 'test.task.type.singleton'
    _singleton = True

    def execute(self, env, task):
        return {}


class TestRoutingAnyServiceType(AbstractTaskType):
    """Routing test: claimable by any service (_service_name=None)."""
    _name = 'test.routing.any.service'
    _service_name = None
    _singleton = False
    _default_channel = 'default'

    def execute(self, env, task):
        return {}


class TestRoutingSpecificServiceType(AbstractTaskType):
    """Routing test: locked to 'my.specific.service'."""
    _name = 'test.routing.specific.service'
    _service_name = 'my.specific.service'
    _singleton = False
    _default_channel = 'specific'

    def execute(self, env, task):
        return {}


class TestRoutingOtherServiceType(AbstractTaskType):
    """Routing test: locked to 'other.service'."""
    _name = 'test.routing.other.service'
    _service_name = 'other.service'
    _singleton = False
    _default_channel = 'other'

    def execute(self, env, task):
        return {}


class TestRoutingCustomChannelType(AbstractTaskType):
    """Routing test: custom _default_channel='heavy'."""
    _name = 'test.routing.custom.channel'
    _service_name = None
    _singleton = False
    _default_channel = 'heavy'

    def execute(self, env, task):
        return {}


class TestTaskTypeTwoPhase(MultiPhaseTaskType):
    """Two-phase pipeline used to test the re-arm primitive and _phases.

    Phase 'a' fans out ``params['a_count']`` echo children (default 2).
    Phase 'b' fans out one echo child per *successful* phase-'a' child,
    demonstrating that prev_results carries only the previous wave.
    """
    _name = 'test.task.type.two.phase'
    _singleton = False
    _phases = ['a', 'b']

    def plan_phase(self, env, task, phase, prev_results):
        if phase == 'a':
            n = task.task_params.get('a_count', 2)
            return ('test.task.type.echo',
                    [{'phase': 'a', 'i': i} for i in range(n)])
        if phase == 'b':
            done = [cr for cr in prev_results if cr.state == 'done']
            return ('test.task.type.echo',
                    [{'phase': 'b', 'i': i} for i in range(len(done))])
        return None

    def aggregate_result(self, env, task, last_results):
        return {'b_children': len(last_results)}


class TestTaskTypeSkipPhase(MultiPhaseTaskType):
    """Multi-phase pipeline whose first phase yields no children.

    Verifies that an empty phase is skipped and the next phase runs.
    """
    _name = 'test.task.type.skip.phase'
    _singleton = False
    _phases = ['empty', 'real']

    def plan_phase(self, env, task, phase, prev_results):
        if phase == 'real':
            return ('test.task.type.echo', [{'x': 1}])
        # 'empty' phase → no children
        return None


class TestTaskTypeEmptyPipeline(MultiPhaseTaskType):
    """Multi-phase pipeline where every phase is empty.

    The pipeline must complete immediately via the normal done path.
    """
    _name = 'test.task.type.empty.pipeline'
    _singleton = False
    _phases = ['nothing']

    def plan_phase(self, env, task, phase, prev_results):
        return None


class TestTaskTypeMixedWave(MultiPhaseTaskType):
    """Single-phase pipeline whose wave mixes two child types.

    Phase 'work' fans out 2 echo 'chunk' children (homogeneous part) plus a
    single 'noop' notify child routed to a custom channel — exercising both
    the (type_code, params) tuple form and the dict-spec form in one wave.
    """
    _name = 'test.task.type.mixed.wave'
    _singleton = False
    _phases = ['work']

    def plan_phase(self, env, task, phase, prev_results):
        # Built via TaskListSpec to exercise the builder end-to-end.
        return (
            TaskListSpec()
            .add('test.task.type.echo', {'kind': 'chunk', 'i': 0})
            .add('test.task.type.echo', {'kind': 'chunk', 'i': 1})
            .add('test.task.type.noop', {'kind': 'notify'},
                 channel='fast', priority=1)
        )

    def aggregate_result(self, env, task, last_results):
        # prev_results spans the whole heterogeneous wave, both types.
        return {'types': sorted({cr.task.type_code for cr in last_results})}


class TestTaskTypeIdentityProbe(MultiPhaseTaskType):
    """Records the (env.uid, env.su) seen by execute() and on_all_children_done
    so tests can assert the framework runs them as the creating user (not
    SUPERUSER). Also proves the protected-field writes in _start_wave /
    action_wait_children succeed under a non-super user (explicit sudo).
    """
    _name = 'test.task.type.identity.probe'
    _singleton = False
    _phases = ['only']
    seen = {}

    def plan_phase(self, env, task, phase, prev_results):
        type(self).seen['execute'] = (env.uid, env.su)
        return ('test.task.type.noop', [{'i': 0}])

    def on_all_children_done(self, env, parent_task):
        type(self).seen['on_all_children_done'] = (env.uid, env.su)
        return super().on_all_children_done(env, parent_task)


class TestTaskTypeReArm(AbstractTaskType):
    """Plain (non-MultiPhase) task type exercising the re-arm primitive.

    execute() spawns wave 1. on_all_children_done() spawns wave 2 the first
    time it runs (returning None, so the parent stays 'waiting'), then on
    the second invocation returns a result so the parent completes. Proves
    re-arm works without the MultiPhaseTaskType machinery.
    """
    _name = 'test.task.type.rearm'
    _singleton = False

    def execute(self, env, task):
        task.spawn_children(
            type_code='test.task.type.noop', params_list=[{'wave': 1}])
        task.action_wait_children()

    def on_all_children_done(self, env, parent_task):
        # First invocation (only wave 1 exists) → spawn wave 2 and re-arm.
        # Second invocation (both waves exist) → finalize.
        if len(parent_task.child_ids) < 2:
            parent_task.spawn_children(
                type_code='test.task.type.noop', params_list=[{'wave': 2}])
            return None
        return {'finished': True}


class TestTaskTypeExtBase(AbstractTaskType):
    """Base definition used to verify extension MRO ordering.

    The class below re-declares the same _name to *extend* it. Because the
    extension is registered after this base (Odoo-style: later modules load
    last), the merged class must resolve the extension's members first, while
    members only the base defines stay reachable.
    """
    _name = 'test.task.type.extended'
    _singleton = False
    _marker = 'base'

    def execute(self, env, task):
        return {'origin': 'base'}

    def base_only(self):
        return 'base-only'


class TestTaskTypeExtOverride(AbstractTaskType):
    """Extension of ``test.task.type.extended``, defined *after* the base.

    Overrides ``execute()`` and ``_marker`` but deliberately does NOT define
    ``base_only()`` — so the merged class must inherit it from the base. The
    ``super().execute()`` call proves the merged MRO chains cooperatively into
    the base definition rather than shadowing it.
    """
    _name = 'test.task.type.extended'
    _singleton = False
    _marker = 'override'

    def execute(self, env, task):
        base = super().execute(env, task)
        return {**base, 'origin': 'override'}


class TestTaskTypeRaisingOnAllDone(AbstractTaskType):
    """Waiting-parent whose on_all_children_done raises.

    ``params['raise_kind']`` selects what it raises so tests can exercise all
    three branches of the round-boundary error handling:

    - ``'error'`` (default) — a genuine bug → parent must FAIL (not silently
      complete as 'done', which would drop the remaining work).
    - ``'transient'`` — a transient DB error → parent must stay 'waiting'
      (re-raised for the poll loop to roll back and retry next cycle).
    - ``'retry'`` — an explicit RetryTask → parent must stay 'waiting'
      (retried on a later poll cycle).
    """
    _name = 'test.task.type.raising.on.all.done'
    _singleton = False

    def execute(self, env, task):
        task.spawn_children(
            type_code='test.task.type.noop', params_list=[{'i': 0}])
        task.action_wait_children()

    def on_all_children_done(self, env, parent_task):
        kind = parent_task.task_params.get('raise_kind', 'error')
        if kind == 'transient':
            import psycopg2.errors
            raise psycopg2.errors.SerializationFailure("simulated transient")
        if kind == 'retry':
            from odoo.addons.generic_task_queue.exceptions import RetryTask
            raise RetryTask()
        raise RuntimeError("planning the next wave blew up")
