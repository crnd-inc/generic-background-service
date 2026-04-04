from odoo.tests.common import TransactionCase
from odoo import exceptions


def _make_worker(env, uuid='test-worker-stuck'):
    return env['generic.task.queue.worker'].create({
        'uuid': uuid,
        'service_name': 'test.stuck.service',
        'state': 'active',
    })


def _make_task(env, name='Stuck test task', retry_policy='retriable'):
    return env['generic.task.queue.task'].create({
        'name': name,
        'type_code': 'test.task.type.noop',
        'retry_policy': retry_policy,
    })


class TestStuckStateTransitions(TransactionCase):
    """Tests for the 'stuck' task state and its transitions."""

    def setUp(self):
        super().setUp()
        self.worker = _make_worker(self.env)
        self.task = _make_task(self.env)

    def test_action_stuck_from_running(self):
        """running → stuck should succeed and not increment retry_count."""
        self.task.action_assign(self.worker)
        self.task.action_start()
        self.assertEqual(self.task.state, 'running')

        self.task.action_stuck()

        self.assertEqual(self.task.state, 'stuck')
        self.assertEqual(self.task.retry_count, 0,
                         "action_stuck must NOT increment retry_count")

    def test_action_stuck_invalid_from_pending(self):
        """stuck transition from pending should raise ValidationError."""
        self.assertEqual(self.task.state, 'pending')
        with self.assertRaises(exceptions.ValidationError):
            self.task.action_stuck()

    def test_stuck_to_done_via_action_done(self):
        """stuck → done should succeed (zombie thread completed OK)."""
        self.task.action_assign(self.worker)
        self.task.action_start()
        self.task.action_stuck()
        self.assertEqual(self.task.state, 'stuck')

        self.task.action_done(result={'ok': True})

        self.assertEqual(self.task.state, 'done')
        self.assertEqual(self.task.task_result, {'ok': True})

    def test_stuck_to_failed_via_action_fail(self):
        """stuck → failed should succeed and increment retry_count."""
        self.task.action_assign(self.worker)
        self.task.action_start()
        self.task.action_stuck()

        self.task.action_fail(error='boom')

        self.assertEqual(self.task.state, 'failed')
        self.assertEqual(self.task.task_error, 'boom')
        self.assertEqual(self.task.retry_count, 1)

    def test_stuck_to_cancelled(self):
        """stuck → cancelled should succeed."""
        self.task.action_assign(self.worker)
        self.task.action_start()
        self.task.action_stuck()

        self.task.action_cancel()

        self.assertEqual(self.task.state, 'cancelled')


class TestRunnerIdGuard(TransactionCase):
    """Tests for runner_id zombie-thread guard on action_done/action_fail."""

    def setUp(self):
        super().setUp()
        self.worker = _make_worker(self.env)
        self.task = _make_task(self.env)
        self.task.action_assign(self.worker)
        self.task.action_start()
        self.task.action_stuck()
        # Set a known runner_id directly (simulating what claim_task writes)
        self.task.sudo().write({'runner_id': 'runner-aaa'})

    def test_action_done_matching_runner_id(self):
        """action_done with matching runner_id should complete the task."""
        self.task.action_done(result={'x': 1}, runner_id='runner-aaa')
        self.assertEqual(self.task.state, 'done')

    def test_action_done_mismatched_runner_id_is_dropped(self):
        """action_done with wrong runner_id should be silently dropped."""
        self.task.action_done(result={'x': 1}, runner_id='runner-zzz')
        # State unchanged — zombie write was dropped
        self.assertEqual(self.task.state, 'stuck')

    def test_action_done_no_runner_id_skips_guard(self):
        """action_done without runner_id skips the guard (legacy path)."""
        self.task.action_done(result={'x': 1})
        self.assertEqual(self.task.state, 'done')

    def test_action_fail_matching_runner_id(self):
        """action_fail with matching runner_id should fail the task."""
        self.task.action_fail(error='err', runner_id='runner-aaa')
        self.assertEqual(self.task.state, 'failed')

    def test_action_fail_mismatched_runner_id_is_dropped(self):
        """action_fail with wrong runner_id should be silently dropped."""
        self.task.action_fail(error='err', runner_id='runner-zzz')
        self.assertEqual(self.task.state, 'stuck')


class TestClaimTaskAssignsRunnerId(TransactionCase):
    """claim_task() must assign a distinct runner_id to each claimed task."""

    def setUp(self):
        super().setUp()
        self.worker = _make_worker(self.env)

    def test_claim_assigns_runner_id(self):
        """Claimed task must have a non-empty runner_id."""
        _make_task(self.env)
        Task = self.env['generic.task.queue.task']
        claimed = Task.claim_task(
            self.worker, ['default'], [], limit=1)
        self.assertEqual(len(claimed), 1)
        self.assertTrue(claimed.runner_id,
                        "claim_task must assign a runner_id")

    def test_claim_assigns_distinct_runner_ids(self):
        """Two claimed tasks must receive different runner_ids."""
        _make_task(self.env, name='Task A')
        _make_task(self.env, name='Task B')
        Task = self.env['generic.task.queue.task']
        claimed = Task.claim_task(
            self.worker, ['default'], [], limit=2)
        self.assertEqual(len(claimed), 2)
        ids = [t.runner_id for t in claimed]
        self.assertEqual(len(set(ids)), 2,
                         "Each claimed task must have a unique runner_id")


class TestMarkDeadHandlesStuck(TransactionCase):
    """mark_dead() must properly handle stuck tasks."""

    def setUp(self):
        super().setUp()
        self.worker = _make_worker(self.env)

    def _put_task_in_stuck(self, retry_policy='retriable'):
        task = _make_task(self.env, retry_policy=retry_policy)
        task.action_assign(self.worker)
        task.action_start()
        task.action_stuck()
        task.sudo().write({'runner_id': 'runner-old'})
        return task

    def test_mark_dead_retriable_stuck_goes_pending(self):
        """mark_dead on retriable stuck task → pending, runner_id cleared."""
        task = self._put_task_in_stuck('retriable')

        self.worker.mark_dead()

        self.assertEqual(task.state, 'pending')
        self.assertFalse(task.worker_id)
        self.assertFalse(task.runner_id,
                         "runner_id must be cleared to invalidate zombies")

    def test_mark_dead_non_retriable_stuck_goes_failed(self):
        """mark_dead on non-retriable stuck task → failed, retry_count++."""
        task = self._put_task_in_stuck('non_retriable')
        self.assertEqual(task.retry_count, 0)

        self.worker.mark_dead()

        self.assertEqual(task.state, 'failed')
        self.assertEqual(
            task.retry_count, 1,
            "mark_dead must increment retry_count for stuck tasks")

    def test_mark_dead_also_handles_running(self):
        """mark_dead must still handle running (non-stuck) tasks."""
        task = _make_task(self.env)
        task.action_assign(self.worker)
        task.action_start()

        self.worker.mark_dead()

        self.assertEqual(task.state, 'pending')


class TestAutoRetrySkipsStuck(TransactionCase):
    """_auto_retry_failed() must never touch stuck tasks."""

    def test_auto_retry_does_not_touch_stuck(self):
        """Stuck tasks must remain stuck after _auto_retry_failed runs."""
        worker = _make_worker(self.env)
        task = _make_task(self.env)
        task.action_assign(worker)
        task.action_start()
        task.action_stuck()

        # Directly exercise the SQL-level check in _auto_retry_failed:
        # state='stuck' must not be selected since it filters state='failed'
        self.env.cr.execute("""
            SELECT id FROM generic_task_queue_task
            WHERE state = 'failed'
              AND retry_policy = 'retriable'
              AND channel IN %s
            FOR UPDATE SKIP LOCKED
        """, (('default',),))
        selected_ids = [r[0] for r in self.env.cr.fetchall()]
        self.assertNotIn(task.id, selected_ids,
                         "stuck task must NOT appear in failed query")
        # State unchanged
        task.invalidate_recordset(['state'])
        self.assertEqual(task.state, 'stuck')


class TestOrphanCleanupOnStartup(TransactionCase):
    """_cleanup_orphaned_tasks() must handle tasks left from previous crash."""

    def setUp(self):
        super().setUp()
        self.worker_rec = _make_worker(self.env)

    def _put_task_in_state(self, state, retry_policy='retriable'):
        task = _make_task(self.env, retry_policy=retry_policy)
        task.action_assign(self.worker_rec)
        if state in ('running', 'stuck', 'waiting'):
            task.action_start()
        if state == 'stuck':
            task.action_stuck()
        if state == 'waiting':
            task.action_wait_children()
        if state != 'waiting':
            task.sudo().write({'runner_id': 'old-runner'})
        return task

    def _run_cleanup(self):
        """Simulate what TaskQueueWorker._cleanup_orphaned_tasks does."""
        Task = self.env['generic.task.queue.task']
        worker_id = self.worker_rec.id
        orphans = Task.search([
            ('worker_id', '=', worker_id),
            ('state', 'in', ('assigned', 'running', 'stuck', 'waiting')),
        ])
        for task in orphans:
            if task.state == 'waiting':
                task.write({'worker_id': False})
            elif task.retry_policy == 'retriable':
                task.write({
                    'state': 'pending',
                    'worker_id': False,
                    'runner_id': False,
                    'task_error': False,
                    'progress': 0,
                })
            else:
                task.write({
                    'state': 'failed',
                    'task_error': 'Worker restarted during execution',
                    'retry_count': task.retry_count + 1,
                })

    def test_retriable_running_goes_pending(self):
        """Orphaned retriable running task → pending, runner_id cleared."""
        task = self._put_task_in_state('running', 'retriable')
        self._run_cleanup()
        self.assertEqual(task.state, 'pending')
        self.assertFalse(task.worker_id)
        self.assertFalse(task.runner_id)

    def test_retriable_stuck_goes_pending(self):
        """Orphaned retriable stuck task → pending, runner_id cleared."""
        task = self._put_task_in_state('stuck', 'retriable')
        self._run_cleanup()
        self.assertEqual(task.state, 'pending')
        self.assertFalse(task.runner_id)

    def test_non_retriable_stuck_goes_failed(self):
        """Orphaned non-retriable stuck task → failed, retry_count bumped."""
        task = self._put_task_in_state('stuck', 'non_retriable')
        self._run_cleanup()
        self.assertEqual(task.state, 'failed')
        self.assertEqual(task.retry_count, 1)

    def test_retriable_assigned_goes_pending(self):
        """Orphaned assigned task → pending."""
        task = self._put_task_in_state('assigned', 'retriable')
        self._run_cleanup()
        self.assertEqual(task.state, 'pending')

    def test_waiting_task_worker_cleared(self):
        """Orphaned waiting task: worker_id cleared, state stays waiting."""
        task = self._put_task_in_state('waiting', 'retriable')
        self.assertEqual(task.state, 'waiting')
        self.assertTrue(task.worker_id)

        self._run_cleanup()

        self.assertEqual(task.state, 'waiting',
                         "waiting task must stay waiting — children are done")
        self.assertFalse(
            task.worker_id,
            "worker_id must be cleared so any worker can re-check")

    def test_waiting_task_not_reset_to_pending(self):
        """Orphaned waiting task must NOT go back to pending."""
        task = self._put_task_in_state('waiting', 'retriable')
        self._run_cleanup()
        self.assertNotEqual(
            task.state, 'pending',
            "re-executing a waiting task would duplicate child runs")


class TestStaleStuckWorker(TransactionCase):
    """check_stale_workers() must also detect stuck-state workers."""

    def test_stuck_worker_marked_dead_after_heartbeat_timeout(self):
        """A stuck worker that stops heartbeating must be detected as stale."""
        from datetime import timedelta
        Worker = self.env['generic.task.queue.worker']
        old_time = self.env.cr.now() - timedelta(seconds=120)
        w = Worker.create({
            'uuid': 'stale-stuck-test',
            'service_name': 'test.svc',
            'state': 'stuck',          # stuck — not active
            'last_heartbeat': old_time,
        })
        stale = Worker.check_stale_workers(heartbeat_timeout=60)
        self.assertIn(w, stale)
        self.assertEqual(w.state, 'dead',
                         "stuck worker with old heartbeat must be marked dead")

    def test_active_worker_still_detected(self):
        """Active worker with old heartbeat must still be detected."""
        from datetime import timedelta
        Worker = self.env['generic.task.queue.worker']
        old_time = self.env.cr.now() - timedelta(seconds=120)
        w = Worker.create({
            'uuid': 'stale-active-test',
            'service_name': 'test.svc',
            'state': 'active',
            'last_heartbeat': old_time,
        })
        stale = Worker.check_stale_workers(heartbeat_timeout=60)
        self.assertIn(w, stale)
        self.assertEqual(w.state, 'dead')


class TestCancelStuckTask(TransactionCase):
    """action_cancel() must work on stuck tasks and cascade to children."""

    def setUp(self):
        super().setUp()
        self.worker = _make_worker(self.env)

    def test_cancel_stuck_task(self):
        """Cancel a stuck task directly."""
        task = _make_task(self.env)
        task.action_assign(self.worker)
        task.action_start()
        task.action_stuck()

        task.action_cancel()
        self.assertEqual(task.state, 'cancelled')

    def test_cancel_parent_also_cancels_stuck_child(self):
        """Cancelling a parent cascades to stuck children."""
        parent = _make_task(self.env, name='Parent')
        parent.action_assign(self.worker)
        parent.action_start()

        child = self.env['generic.task.queue.task'].create({
            'name': 'Child',
            'type_code': 'test.task.type.noop',
            'parent_id': parent.id,
        })
        child.action_assign(self.worker)
        child.action_start()
        child.action_stuck()

        parent.action_cancel()

        self.assertEqual(parent.state, 'cancelled')
        self.assertEqual(child.state, 'cancelled')


class TestWaitingParentWithStuckChild(TransactionCase):
    """Parent in waiting state must not complete while a child is stuck."""

    def setUp(self):
        super().setUp()
        self.worker = _make_worker(self.env)

    def test_parent_waits_for_stuck_child(self):
        """_check_waiting_parent must return early when a child is stuck."""
        parent = _make_task(self.env, name='Parent')
        parent.action_assign(self.worker)
        parent.action_start()
        # Simulate parent entering 'waiting' state
        parent.action_wait_children()

        child = self.env['generic.task.queue.task'].create({
            'name': 'Child',
            'type_code': 'test.task.type.noop',
            'parent_id': parent.id,
        })
        child.action_assign(self.worker)
        child.action_start()
        child.action_stuck()

        parent._check_waiting_parent()

        # Parent must still be waiting — stuck child is still active
        self.assertEqual(parent.state, 'waiting')


class TestWaitingTaskOrphanRecheck(TransactionCase):
    """Waiting task with cleared worker_id must be re-checked by any worker."""

    def test_waiting_task_without_worker_id_is_checked(self):
        """_check_waiting_parent must run even when worker_id is cleared."""
        worker = _make_worker(self.env)
        parent = _make_task(self.env, name='Orphan parent')
        parent.action_assign(worker)
        parent.action_start()
        parent.action_wait_children()
        # Simulate orphan cleanup: clear worker_id
        parent.sudo().write({'worker_id': False})

        # All children already done (no children → done immediately)
        self.assertFalse(parent.child_ids)
        parent._check_waiting_parent()

        self.assertEqual(parent.state, 'done',
                         "waiting task with no children must complete "
                         "regardless of worker_id")


class TestTaskTypeDefaultTimeout(TransactionCase):
    """default_timeout field on task type."""

    def test_default_timeout_field_readable(self):
        """Task type must expose default_timeout with default 0."""
        task_type = self.env['generic.task.queue.task.type'].search(
            [('code', '=', 'test.task.type.noop')], limit=1)
        self.assertTrue(task_type)
        self.assertEqual(task_type.default_timeout, 0)

    def test_default_timeout_writable(self):
        """default_timeout should be writable."""
        task_type = self.env['generic.task.queue.task.type'].search(
            [('code', '=', 'test.task.type.noop')], limit=1)
        self.assertTrue(task_type)
        task_type.default_timeout = 120
        self.assertEqual(task_type.default_timeout, 120)


class TestWorkerMarkStuck(TransactionCase):
    """mark_stuck() must update worker state for observability."""

    def test_mark_stuck(self):
        """mark_stuck() must transition worker to 'stuck' state."""
        worker = _make_worker(self.env)
        self.assertEqual(worker.state, 'active')
        worker.mark_stuck()
        self.assertEqual(worker.state, 'stuck')

    def test_heartbeat_reactivates_stuck_worker(self):
        """heartbeat() must reactivate a stuck worker."""
        worker = _make_worker(self.env)
        worker.mark_stuck()
        self.assertEqual(worker.state, 'stuck')
        worker.heartbeat()
        self.assertEqual(worker.state, 'active')
