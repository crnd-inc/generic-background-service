from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.generic_task_queue.service.task_type_registry import (
    TaskTypeRegistry,
)

from .common import make_service_worker, recover_dead_worker

TASK_MODEL_LOGGER = (
    'odoo.addons.generic_task_queue.models.generic_task_queue_task')


def _record_on_failure(test, task_type_cls):
    """Capture the exception types a task type's on_failure hook receives.

    Patched only for the calling test. Only the exception *type* is kept —
    a caught exception would pin its traceback and, through it, the failed
    transaction's env, cursor and recordsets.

    :return: list filled with ``type(exc)`` as the hook fires
    """
    seen = []

    def _on_failure(self, env, task, exc):
        seen.append(type(exc))

    patcher = patch.object(task_type_cls, 'on_failure', _on_failure)
    patcher.start()
    test.addCleanup(patcher.stop)
    return seen


class TestWorkerModel(TransactionCase):
    """Test the generic.task.queue.worker model."""

    def test_worker_name_computed(self):
        """name should be '{service_name} @ {hostname}' and recompute
        when either field changes."""
        Worker = self.env['generic.task.queue.worker']
        w = Worker.create({
            'uuid': 'name-test-1',
            'service_name': 'my.service',
            'hostname': 'myhost',
        })
        self.assertEqual(w.name, 'my.service @ myhost')

        w.write({'service_name': 'other.service'})
        self.assertEqual(w.name, 'other.service @ myhost')

        w.write({'hostname': 'newhost'})
        self.assertEqual(w.name, 'other.service @ newhost')

    def test_worker_name_unknown_fallback(self):
        """name falls back to 'unknown' when service_name or hostname unset."""
        Worker = self.env['generic.task.queue.worker']
        w = Worker.create({'uuid': 'name-test-fallback'})
        self.assertEqual(w.name, 'unknown @ unknown')

    def test_find_or_create_new(self):
        """find_or_create should create a new record."""
        Worker = self.env['generic.task.queue.worker']
        w = Worker.find_or_create(
            service_name='test.svc',
            dbname='testdb',
            uuid='uuid-1',
            channels='default',
            task_types='task.type.model.method',
            max_parallel_jobs=2,
        )
        self.assertTrue(w.id)
        self.assertEqual(w.service_name, 'test.svc')
        self.assertEqual(w.uuid, 'uuid-1')
        self.assertEqual(w.state, 'active')
        self.assertEqual(w.max_parallel_jobs, 2)

    def test_find_or_create_reuses_existing(self):
        """find_or_create should reuse an existing record
        for the same service_name + dbname."""
        Worker = self.env['generic.task.queue.worker']
        w1 = Worker.find_or_create(
            service_name='test.svc',
            dbname='testdb',
            uuid='uuid-1',
            channels='default',
            task_types='task.type.model.method',
            max_parallel_jobs=1,
        )
        w2 = Worker.find_or_create(
            service_name='test.svc',
            dbname='testdb',
            uuid='uuid-2',
            channels='default,heavy',
            task_types='task.type.model.method',
            max_parallel_jobs=3,
        )
        self.assertEqual(w1.id, w2.id)
        self.assertEqual(w2.uuid, 'uuid-2')
        self.assertEqual(w2.max_parallel_jobs, 3)

    def test_heartbeat(self):
        """heartbeat() should update last_heartbeat."""
        Worker = self.env['generic.task.queue.worker']
        w = Worker.create({
            'uuid': 'hb-test',
            'service_name': 'test.svc',
            'state': 'stuck',
        })
        w.heartbeat()
        self.assertEqual(w.state, 'active')
        self.assertTrue(w.last_heartbeat)

    def test_dead_worker_reassigns_retriable(self):
        """Dead-worker recovery should requeue retriable tasks with budget
        consuming one retry attempt."""
        Worker = self.env['generic.task.queue.worker']
        Task = self.env['generic.task.queue.task']
        w = Worker.create({
            'uuid': 'dead-test',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = Task.create({
            'name': 'Stuck task',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'retry_any',
            'max_retries': 3,
        })
        task.action_assign(w)
        task.action_start()
        self.assertEqual(task.state, 'running')

        recover_dead_worker(self.env, w)
        self.assertEqual(w.state, 'dead')
        self.assertEqual(task.state, 'pending')
        self.assertFalse(task.worker_id)
        self.assertEqual(task.retry_count, 1)

    @mute_logger(TASK_MODEL_LOGGER)
    def test_one_bad_task_does_not_abort_recovery(self):
        """A task whose recovery raises must not abort recovery of its
        siblings; a later sweep picks it up."""
        Worker = self.env['generic.task.queue.worker']
        Task = self.env['generic.task.queue.task']
        w = Worker.create({
            'uuid': 'dead-test-guard',
            'service_name': 'test.svc',
            'state': 'active',
        })
        tasks = Task.create([{
            'name': 'Orphan %d' % i,
            'type_code': 'test.task.type.noop',
            'retry_policy': 'no_retry',
        } for i in range(2)])
        for task in tasks:
            task.action_assign(w)
            task.action_start()
        bad, good = tasks
        w.mark_dead()

        RegistryTask = self.env.registry['generic.task.queue.task']
        orig = RegistryTask._handle_lost_execution

        def _failing(rec, error, **kwargs):
            if rec.id == bad.id:
                raise RuntimeError('simulated recovery failure')
            return orig(rec, error, **kwargs)

        with patch.object(
                RegistryTask, '_handle_lost_execution', _failing):
            make_service_worker(self.env)._recover_dead_worker_tasks()

        self.assertEqual(bad.state, 'running')      # left for the sweep
        self.assertEqual(good.state, 'failed')

        make_service_worker(self.env)._recover_dead_worker_tasks()
        self.assertEqual(bad.state, 'failed')

    def test_dead_worker_fails_non_retriable(self):
        """Dead-worker recovery should fail non-retriable tasks."""
        Worker = self.env['generic.task.queue.worker']
        Task = self.env['generic.task.queue.task']
        w = Worker.create({
            'uuid': 'dead-test-2',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = Task.create({
            'name': 'Non-retriable',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'no_retry',
        })
        task.action_assign(w)
        task.action_start()

        recover_dead_worker(self.env, w)
        self.assertEqual(task.state, 'failed')
        self.assertIn('Worker died', task.task_error)

    def test_dead_worker_runs_on_failure_hook(self):
        """Failing an orphaned task must run on_failure and go through
        action_fail(). Task types finalize external bookkeeping in on_failure
        (e.g. marking an owning migration record failed); a raw state write
        would leave that record stranded, with no date_completed and no
        completion notification."""
        from odoo.addons.generic_task_queue.exceptions import (
            TaskAbandonedError,
        )
        from ..service.test_task_types import TestTaskTypeNoOp
        Worker = self.env['generic.task.queue.worker']
        Task = self.env['generic.task.queue.task']
        w = Worker.create({
            'uuid': 'dead-test-hook',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = Task.create({
            'name': 'Non-retriable with hook',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'no_retry',
        })
        task.action_assign(w)
        task.action_start()
        seen = _record_on_failure(self, TestTaskTypeNoOp)

        recover_dead_worker(self.env, w)

        self.assertEqual(task.state, 'failed')
        self.assertTrue(task.date_completed)
        self.assertEqual(seen, [TaskAbandonedError])

    def test_dead_worker_requeues_task_that_never_started(self):
        """A task still 'assigned' when its worker died never ran: it goes
        back to pending for any worker without consuming retry budget,
        even under no_retry."""
        Worker = self.env['generic.task.queue.worker']
        Task = self.env['generic.task.queue.task']
        w = Worker.create({
            'uuid': 'dead-test-assigned',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = Task.create({
            'name': 'Assigned but never started',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'no_retry',
        })
        task.action_assign(w)
        task.sudo().write({'runner_id': 'old-runner'})
        self.assertEqual(task.state, 'assigned')

        recover_dead_worker(self.env, w)

        self.assertEqual(task.state, 'pending')
        self.assertEqual(task.retry_count, 0)
        self.assertFalse(task.worker_id)
        self.assertFalse(task.runner_id)

    def test_sweep_recovers_leftover_of_dead_worker(self):
        """In-flight tasks behind a dead worker are recovered by the
        sweep, honouring the retry budget."""
        Worker = self.env['generic.task.queue.worker']
        Task = self.env['generic.task.queue.task']
        w = Worker.create({
            'uuid': 'sweep-test-worker',
            'service_name': 'test.svc',
            'state': 'active',
        })
        retriable = Task.create({
            'name': 'Leftover retriable',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'retry_any',
            'max_retries': 3,
        })
        dead_end = Task.create({
            'name': 'Leftover non-retriable',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'no_retry',
        })
        for task in retriable + dead_end:
            task.action_assign(w)
            task.action_start()
        w.mark_dead()

        make_service_worker(self.env)._recover_dead_worker_tasks()

        self.assertEqual(retriable.state, 'pending')
        self.assertEqual(retriable.retry_count, 1)
        self.assertEqual(dead_end.state, 'failed')

    def test_sweep_leaves_re_claimed_task_alone(self):
        """A task requeued and re-claimed by a live worker after the sweep
        found it (e.g. by a sibling's sweep) must not be recovered again:
        the worker and runner captured at search time are verified under
        the lock."""
        Worker = self.env['generic.task.queue.worker']
        Task = self.env['generic.task.queue.task']
        dead = Worker.create({
            'uuid': 'sweep-race-dead',
            'service_name': 'test.svc',
            'state': 'dead',
        })
        live = Worker.create({
            'uuid': 'sweep-race-live',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = Task.create({
            'name': 'Re-claimed meanwhile',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'retry_any',
            'max_retries': 3,
        })
        task.action_assign(dead)
        task.action_start()
        task.sudo().write({'runner_id': 'old-runner'})

        RegistryTask = self.env.registry['generic.task.queue.task']
        orig = RegistryTask._handle_lost_execution

        def _reclaim_then_recover(rec, error, **kwargs):
            # Another worker claimed the task between the sweep's search
            # and this task's recovery.
            rec.sudo().write({'worker_id': live.id, 'runner_id': 'new'})
            return orig(rec, error, **kwargs)

        with patch.object(
                RegistryTask, '_handle_lost_execution',
                _reclaim_then_recover):
            make_service_worker(self.env)._recover_dead_worker_tasks()

        self.assertEqual(task.state, 'running')
        self.assertEqual(task.worker_id, live)
        self.assertEqual(task.runner_id, 'new')
        self.assertEqual(task.retry_count, 0)

    def test_sweep_ignores_tasks_of_live_workers(self):
        Worker = self.env['generic.task.queue.worker']
        Task = self.env['generic.task.queue.task']
        w = Worker.create({
            'uuid': 'sweep-test-live',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = Task.create({
            'name': 'Healthy running task',
            'type_code': 'test.task.type.noop',
        })
        task.action_assign(w)
        task.action_start()

        make_service_worker(self.env)._recover_dead_worker_tasks()

        self.assertEqual(task.state, 'running')

    def test_stale_peer_check_recovers_its_tasks(self):
        """The stale-peer check marks a silent peer dead and the sweep it
        triggers recovers that peer's in-flight tasks."""
        from datetime import timedelta
        Worker = self.env['generic.task.queue.worker']
        Task = self.env['generic.task.queue.task']
        w = Worker.create({
            'uuid': 'stale-peer',
            'service_name': 'test.svc',
            'state': 'active',
            'last_heartbeat': self.env.cr.now() - timedelta(seconds=120),
        })
        task = Task.create({
            'name': 'Leftover',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'no_retry',
        })
        task.action_assign(w)
        task.action_start()

        make_service_worker(self.env)._check_stale_peers()

        self.assertEqual(w.state, 'dead')
        self.assertEqual(task.state, 'failed')

    def test_check_stale_workers(self):
        """check_stale_workers() should detect and mark dead workers
        that missed heartbeat."""
        Worker = self.env['generic.task.queue.worker']
        from datetime import timedelta
        old_time = self.env.cr.now() - timedelta(seconds=120)
        w = Worker.create({
            'uuid': 'stale-test',
            'service_name': 'test.svc',
            'state': 'active',
            'last_heartbeat': old_time,
        })
        stale = Worker.check_stale_workers(heartbeat_timeout=60)
        self.assertIn(w, stale)
        self.assertEqual(w.state, 'dead')


class TestTaskExecution(TransactionCase):
    """Test task execution via the task type registry."""

    def test_noop_task_type_executes(self):
        """TestTaskTypeNoOp should return {'status': 'noop'}."""
        registry = TaskTypeRegistry()
        cls = registry.get_task_type('test.task.type.noop')
        task_type = cls()

        Task = self.env['generic.task.queue.task']
        task = Task.create({
            'name': 'Noop test',
            'type_code': 'test.task.type.noop',
        })
        result = task_type.execute(self.env, task)
        self.assertEqual(result, {'status': 'noop'})

    def test_model_method_via_task_record(self):
        """End-to-end: create task → execute ModelMethodTaskType."""
        registry = TaskTypeRegistry()
        cls = registry.get_task_type('task.type.model.method')
        task_type = cls()

        Target = self.env['test.task.target']
        rec = Target.create({'name': 'e2e', 'value': 0})

        Task = self.env['generic.task.queue.task']
        task = Task.create({
            'name': 'E2E model method',
            'type_code': 'task.type.model.method',
            'task_params': {
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [rec.id],
                'kwargs': {'amount': 7},
            },
        })
        task_type.execute(self.env, task)
        rec.invalidate_recordset()
        self.assertEqual(rec.value, 7)
        self.assertTrue(rec.processed)


class TestTaskTimeout(TransactionCase):
    """Test the timeout field on tasks."""

    def test_timeout_field_default(self):
        """Default timeout should be 0 (no timeout)."""
        Task = self.env['generic.task.queue.task']
        task = Task.create({
            'name': 'Timeout default',
            'type_code': 'test.task.type.noop',
        })
        self.assertEqual(task.timeout, 0)

    def test_timeout_field_custom(self):
        """Custom timeout should be stored."""
        Task = self.env['generic.task.queue.task']
        task = Task.create({
            'name': 'Timeout custom',
            'type_code': 'test.task.type.noop',
            'timeout': 3600,
        })
        self.assertEqual(task.timeout, 3600)


class TestTimeoutResolutionChain(TransactionCase):
    """Timeout is resolved: task → task type default → service default (3600s).

    The service-level default acts as a safety net so tasks that neither
    specify a per-task timeout nor a task-type default_timeout don't run
    indefinitely. Task types that genuinely need unbounded execution time
    must set default_timeout=0 to opt out explicitly.
    """

    def _resolve(self, task_timeout, type_timeout, worker_default):
        """Replicate the resolution logic from
           TaskQueueWorker._claim_and_spawn."""
        return (
            task_timeout if task_timeout > 0
            else type_timeout if type_timeout > 0
            else worker_default
        )

    def test_task_timeout_wins(self):
        """Per-task timeout takes priority over everything."""
        self.assertEqual(self._resolve(120, 300, 3600), 120)

    def test_type_default_wins_over_service_default(self):
        """Task-type default_timeout takes priority over service default."""
        self.assertEqual(self._resolve(0, 300, 3600), 300)

    def test_service_default_is_fallback(self):
        """Service default (3600s) is used when task and type have no timeout.
        """
        self.assertEqual(self._resolve(0, 0, 3600), 3600)

    def test_type_zero_falls_through_to_service_default(self):
        """type_timeout=0 does NOT bypass the service default — it falls
        through to it.  A task type alone cannot opt out of the service-level
        safety net; the service must also set _default_task_timeout=0.
        """
        self.assertEqual(self._resolve(0, 0, 3600), 3600)

    def test_both_zero_opts_out_of_all_timeouts(self):
        """Only when BOTH type default and service default are 0 does the
        resolution chain produce 0 (no timeout at all)."""
        self.assertEqual(self._resolve(0, 0, 0), 0)

    def test_task_queue_service_default_is_3600(self):
        """TaskQueueService._default_task_timeout must be 3600 (1 hour)."""
        from odoo.addons.generic_task_queue.service.task_queue_service import (
            TaskQueueService,
        )
        self.assertEqual(TaskQueueService._default_task_timeout, 3600)

    def test_task_queue_service_die_on_stuck_timeout(self):
        """TaskQueueService._die_on_stuck_timeout must be 300 (5 minutes)."""
        from odoo.addons.generic_task_queue.service.task_queue_service import (
            TaskQueueService,
        )
        self.assertEqual(TaskQueueService._die_on_stuck_timeout, 300)


class TestWorkerIsStuck(TransactionCase):
    """Unit tests for TaskQueueWorker.is_stuck() using minimal fakes."""

    def _make_task_info(self, timed_out, alive):
        """Return a minimal _TaskThread-like object."""
        import threading as _threading
        from odoo.addons.generic_task_queue.service.task_queue_worker import (
            _TaskThread,
        )
        thread = _threading.Thread(target=lambda: None)
        if alive:
            thread.start()
            # thread finishes almost immediately; keep reference only
        task_info = _TaskThread(
            task_id=1, thread=thread, timeout=1, runner_id='r')
        task_info.timed_out = timed_out
        return task_info

    def _make_worker(self, max_parallel_jobs=1):
        """Return a TaskQueueWorker with no DB connection."""
        from odoo.addons.generic_task_queue.service.task_queue_worker import (
            TaskQueueWorker,
        )
        worker = TaskQueueWorker.__new__(TaskQueueWorker)
        worker._max_parallel_jobs = max_parallel_jobs
        worker._active_tasks = []
        return worker

    def test_is_stuck_no_tasks(self):
        """Worker with no active tasks is not stuck."""
        worker = self._make_worker()
        self.assertFalse(worker.is_stuck())

    def test_is_stuck_all_slots_stuck(self):
        """All slots occupied by timed-out live threads → stuck."""
        import threading as _threading
        from odoo.addons.generic_task_queue.service.task_queue_worker import (
            _TaskThread,
        )
        worker = self._make_worker(max_parallel_jobs=1)
        # Build a thread that blocks until we release it
        barrier = _threading.Barrier(2)

        def _block():
            barrier.wait()

        thread = _threading.Thread(target=_block, daemon=True)
        thread.start()
        task_info = _TaskThread(
            task_id=1, thread=thread, timeout=1, runner_id='r')
        task_info.timed_out = True
        worker._active_tasks = [task_info]
        try:
            self.assertTrue(worker.is_stuck())
        finally:
            barrier.wait()
            thread.join(timeout=2)

    def test_is_stuck_some_timed_out_but_free_slots(self):
        """Fewer stuck threads than max_parallel_jobs → not stuck."""
        import threading as _threading
        from odoo.addons.generic_task_queue.service.task_queue_worker import (
            _TaskThread,
        )
        worker = self._make_worker(max_parallel_jobs=2)
        barrier = _threading.Barrier(2)

        def _block():
            barrier.wait()

        thread = _threading.Thread(target=_block, daemon=True)
        thread.start()
        task_info = _TaskThread(
            task_id=1, thread=thread, timeout=1, runner_id='r')
        task_info.timed_out = True
        # Only 1 stuck thread but max_parallel_jobs=2 → not stuck
        worker._active_tasks = [task_info]
        try:
            self.assertFalse(worker.is_stuck())
        finally:
            barrier.wait()
            thread.join(timeout=2)

    def test_is_stuck_timed_out_but_thread_dead(self):
        """Timed-out thread that already finished → not stuck."""
        from odoo.addons.generic_task_queue.service.task_queue_worker import (
            _TaskThread,
        )
        import threading as _threading
        worker = self._make_worker(max_parallel_jobs=1)
        thread = _threading.Thread(target=lambda: None)
        thread.start()
        thread.join()  # Let it finish
        task_info = _TaskThread(
            task_id=1, thread=thread, timeout=1, runner_id='r')
        task_info.timed_out = True
        worker._active_tasks = [task_info]
        self.assertFalse(worker.is_stuck())


class TestResolvedStuckThreads(TransactionCase):
    """TaskQueueWorker._handle_resolved_stuck_threads: a task thread that
    exited after its timeout without writing a final state."""

    def _make_task_info(self, task_id, runner_id):
        import threading
        from odoo.addons.generic_task_queue.service.task_queue_worker import (
            _TaskThread,
        )
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join(timeout=2)
        task_info = _TaskThread(
            task_id=task_id, thread=thread, timeout=1, runner_id=runner_id)
        task_info.timed_out = True
        return task_info

    def test_abandoned_thread_runs_on_failure_hook(self):
        """A non-retriable task left 'stuck' by a dead thread must be failed
        through action_fail() with on_failure run first, so task types can
        finalize external bookkeeping."""
        from odoo.addons.generic_task_queue.exceptions import (
            TaskAbandonedError,
        )
        from ..service.test_task_types import TestTaskTypeNoOp
        w = self.env['generic.task.queue.worker'].create({
            'uuid': 'stuck-thread-worker',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = self.env['generic.task.queue.task'].create({
            'name': 'Abandoned',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'no_retry',
        })
        task.action_assign(w)
        task.action_start()
        task.action_stuck()
        self.assertEqual(task.state, 'stuck')
        seen = _record_on_failure(self, TestTaskTypeNoOp)

        worker = make_service_worker(self.env)
        worker._handle_resolved_stuck_threads(
            [self._make_task_info(task.id, task.runner_id)])

        task.invalidate_recordset(['state', 'date_completed'])
        self.assertEqual(task.state, 'failed')
        self.assertTrue(task.date_completed)
        self.assertEqual(seen, [TaskAbandonedError])

    def test_recovery_leaves_re_claimed_execution_alone(self):
        """The runner check in _handle_resolved_stuck_threads is unlocked,
        so a task requeued by a peer and re-claimed by another worker in
        the window before the lock must not be recovered here: that
        execution belongs to its new runner, and recovering it would burn
        a retry or fail a task that is legitimately running."""
        from ..service.test_task_types import TestTaskTypeNoOp
        seen = _record_on_failure(self, TestTaskTypeNoOp)
        w = self.env['generic.task.queue.worker'].create({
            'uuid': 'stuck-thread-reclaimed',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = self.env['generic.task.queue.task'].create({
            'name': 'Re-claimed while stuck',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'retry_any',
            'max_retries': 3,
        })
        task.action_assign(w)
        task.action_start()
        # The new owner's execution, as it looks after requeue + re-claim.
        task.sudo().write({'runner_id': 'new-runner'})

        task.sudo()._handle_lost_execution(
            'should be ignored', runner_id='stale-runner')

        task.invalidate_recordset()
        self.assertEqual(task.state, 'running')
        self.assertEqual(task.retry_count, 0)
        self.assertEqual(task.runner_id, 'new-runner')
        self.assertEqual(seen, [])

    def test_recovery_proceeds_for_matching_runner(self):
        """Positive control: the runner check must not block the ordinary
        recovery of the execution the caller actually owns."""
        w = self.env['generic.task.queue.worker'].create({
            'uuid': 'stuck-thread-matching',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = self.env['generic.task.queue.task'].create({
            'name': 'Own execution',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'retry_any',
            'max_retries': 3,
        })
        task.action_assign(w)
        task.action_start()
        task.sudo().write({'runner_id': 'live-runner'})

        task.sudo()._handle_lost_execution(
            'thread exited', runner_id='live-runner')

        task.invalidate_recordset()
        self.assertEqual(task.state, 'pending')
        self.assertEqual(task.retry_count, 1)

    def test_recovery_without_runner_checks_state_only(self):
        """A caller with no execution identity (whole-worker recovery, or
        a task that never got a runner stamped) must still recover: an
        unset runner_id reads as False in the ORM and must not reach the
        query as a boolean."""
        w = self.env['generic.task.queue.worker'].create({
            'uuid': 'stuck-thread-no-runner',
            'service_name': 'test.svc',
            'state': 'active',
        })
        task = self.env['generic.task.queue.task'].create({
            'name': 'No runner stamped',
            'type_code': 'test.task.type.noop',
            'retry_policy': 'retry_any',
            'max_retries': 3,
        })
        task.action_assign(w)
        task.action_start()
        self.assertFalse(task.runner_id)

        task.sudo()._handle_lost_execution(
            'thread exited', runner_id=task.runner_id)

        task.invalidate_recordset()
        self.assertEqual(task.state, 'pending')
        self.assertEqual(task.retry_count, 1)


class TestCleanupOrphanedTasks(TransactionCase):
    """TaskQueueWorker._cleanup_orphaned_tasks: startup recovery after an
    unclean stop (SIGKILL, OOM, container restart)."""

    def setUp(self):
        super().setUp()
        self.worker = make_service_worker(self.env)
        self.worker_rec = self.env['generic.task.queue.worker'].create({
            'uuid': 'cleanup-test-worker',
            'service_name': 'test.svc',
            'state': 'active',
        })

    def _make_running_task(self, retry_policy, max_retries=0):
        task = self.env['generic.task.queue.task'].create({
            'name': 'Orphan',
            'type_code': 'test.task.type.noop',
            'retry_policy': retry_policy,
            'max_retries': max_retries,
        })
        task.action_assign(self.worker_rec)
        task.action_start()
        return task

    def test_cleanup_requeues_retriable_with_budget(self):
        task = self._make_running_task('retry_known', max_retries=3)

        self.worker._cleanup_orphaned_tasks(self.worker_rec.id)

        self.assertEqual(task.state, 'pending')
        self.assertFalse(task.worker_id)
        self.assertEqual(task.retry_count, 1,
                         "a lost run must consume a retry attempt")

    def test_cleanup_fails_non_retriable_with_hook(self):
        """The likeliest crash path for a no_retry type: worker restart
        mid-run. Must fail through on_failure + action_fail, not a raw
        state write."""
        from odoo.addons.generic_task_queue.exceptions import (
            TaskAbandonedError,
        )
        from ..service.test_task_types import TestTaskTypeNoOp
        task = self._make_running_task('no_retry')
        seen = _record_on_failure(self, TestTaskTypeNoOp)

        self.worker._cleanup_orphaned_tasks(self.worker_rec.id)

        self.assertEqual(task.state, 'failed')
        self.assertTrue(task.date_completed)
        self.assertIn('Worker restarted', task.task_error)
        self.assertEqual(seen, [TaskAbandonedError])

    def test_cleanup_fails_retriable_with_exhausted_budget(self):
        task = self._make_running_task('retry_any', max_retries=2)
        task.sudo().write({'retry_count': 2})

        self.worker._cleanup_orphaned_tasks(self.worker_rec.id)

        self.assertEqual(task.state, 'failed')

    def test_cleanup_unowns_waiting_parent_only(self):
        """A waiting parent is not re-executed — its children already ran.
        Only worker_id is cleared so any worker can re-check it."""
        parent = self.env['generic.task.queue.task'].create_task(
            'test.task.type.noop', name='Waiting orphan')
        parent.sudo().action_assign(self.worker_rec)
        parent.sudo().action_start()
        child = self.env['generic.task.queue.task'].create_task(
            'test.task.type.noop', name='Child', parent_id=parent.id)
        parent.sudo().action_wait_children()
        self.assertEqual(parent.state, 'waiting')

        self.worker._cleanup_orphaned_tasks(self.worker_rec.id)

        self.assertEqual(parent.state, 'waiting')
        self.assertFalse(parent.worker_id)
        self.assertEqual(child.state, 'pending')
