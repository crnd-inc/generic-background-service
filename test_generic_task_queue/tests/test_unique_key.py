"""Tests for the unique_key deduplication guard on create_task().

Covers:
  - reuse-running (default): returns existing active task
  - raise: AlreadyScheduledException carries the conflicting task
  - all active states are blocked (pending, assigned, running, stuck, waiting)
  - terminal tasks do not block re-enqueueing
  - tasks without unique_key are never deduplicated
  - unique_key is stored on the task record
  - concurrent-race path: UniqueViolation from savepoint → fallback SELECT
"""
import psycopg2.errors

from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.exceptions import (
    AlreadyScheduledException,
)


class TestUniqueKeyDedup(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.worker = self.env['generic.task.queue.worker'].create({
            'uuid': 'unique-key-test-worker',
            'service_name': 'test.service',
            'state': 'active',
        })

    # ------------------------------------------------------------------
    # reuse-running policy
    # ------------------------------------------------------------------

    def test_reuse_running_returns_existing_pending_task(self):
        """Second create_task with same unique_key returns the existing
        pending task instead of creating a new one."""
        t1 = self.Task.create_task(
            'test.task.type.noop', name='First',
            unique_key='my-key')
        t2 = self.Task.create_task(
            'test.task.type.noop', name='Second',
            unique_key='my-key')

        self.assertEqual(t1.id, t2.id)
        count = self.Task.search_count([('unique_key', '=', 'my-key')])
        self.assertEqual(count, 1)

    def test_reuse_running_is_default_policy(self):
        """on_conflict defaults to 'reuse-running' without explicit arg."""
        t1 = self.Task.create_task(
            'test.task.type.noop', unique_key='default-policy-key')
        t2 = self.Task.create_task(
            'test.task.type.noop', unique_key='default-policy-key')
        self.assertEqual(t1.id, t2.id)

    def test_reuse_running_during_running_state(self):
        """reuse-running returns the task even when it is already running."""
        task = self.Task.create_task(
            'test.task.type.noop', unique_key='running-key')
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()
        self.assertEqual(task.state, 'running')

        t2 = self.Task.create_task(
            'test.task.type.noop', unique_key='running-key')
        self.assertEqual(task.id, t2.id)

    def test_reuse_running_during_waiting_state(self):
        """reuse-running returns the task when it is waiting for children."""
        task = self.Task.create_task(
            'test.task.type.noop', unique_key='waiting-key')
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()
        task.sudo().action_wait_children()
        self.assertEqual(task.state, 'waiting')

        t2 = self.Task.create_task(
            'test.task.type.noop', unique_key='waiting-key')
        self.assertEqual(task.id, t2.id)

    # ------------------------------------------------------------------
    # raise policy
    # ------------------------------------------------------------------

    def test_raise_policy_raises_exception(self):
        """on_conflict='raise' raises AlreadyScheduledException."""
        self.Task.create_task(
            'test.task.type.noop', unique_key='raise-key')

        with self.assertRaises(AlreadyScheduledException):
            self.Task.create_task(
                'test.task.type.noop', unique_key='raise-key',
                on_conflict='raise')

    def test_raise_exception_carries_existing_task(self):
        """AlreadyScheduledException.task points to the existing record."""
        t1 = self.Task.create_task(
            'test.task.type.noop', unique_key='exc-task-key')

        try:
            self.Task.create_task(
                'test.task.type.noop', unique_key='exc-task-key',
                on_conflict='raise')
            self.fail("Expected AlreadyScheduledException")
        except AlreadyScheduledException as exc:
            self.assertEqual(exc.task.id, t1.id)
            self.assertEqual(exc.task.unique_key, 'exc-task-key')

    # ------------------------------------------------------------------
    # Re-enqueueing after completion
    # ------------------------------------------------------------------

    def test_can_reenqueue_after_task_done(self):
        """Same unique_key can be used again once the previous task is done."""
        task = self.Task.create_task(
            'test.task.type.noop', unique_key='reuse-after-done')
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()
        task.sudo().action_done({})
        self.assertEqual(task.state, 'done')

        t2 = self.Task.create_task(
            'test.task.type.noop', unique_key='reuse-after-done')
        self.assertNotEqual(task.id, t2.id)
        self.assertEqual(t2.state, 'pending')

    def test_can_reenqueue_after_task_failed(self):
        """Same unique_key can be used again once the previous task failed."""
        task = self.Task.create_task(
            'test.task.type.noop', unique_key='reuse-after-fail')
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()
        task.sudo().action_fail('boom')

        t2 = self.Task.create_task(
            'test.task.type.noop', unique_key='reuse-after-fail')
        self.assertNotEqual(task.id, t2.id)
        self.assertEqual(t2.state, 'pending')

    def test_can_reenqueue_after_task_cancelled(self):
        """Same unique_key can be used again once the previous task
        is cancelled."""
        task = self.Task.create_task(
            'test.task.type.noop', unique_key='reuse-after-cancel')
        task.action_cancel()

        t2 = self.Task.create_task(
            'test.task.type.noop', unique_key='reuse-after-cancel')
        self.assertNotEqual(task.id, t2.id)
        self.assertEqual(t2.state, 'pending')

    # ------------------------------------------------------------------
    # No unique_key — no deduplication
    # ------------------------------------------------------------------

    def test_no_unique_key_always_creates_new_task(self):
        """Without unique_key, multiple tasks with the same params
        are always created independently."""
        t1 = self.Task.create_task('test.task.type.noop', name='dup')
        t2 = self.Task.create_task('test.task.type.noop', name='dup')
        self.assertNotEqual(t1.id, t2.id)

    # ------------------------------------------------------------------
    # Field stored on record
    # ------------------------------------------------------------------

    def test_unique_key_stored_on_task(self):
        """unique_key is persisted on the created task record."""
        task = self.Task.create_task(
            'test.task.type.noop', unique_key='stored-key')
        self.assertEqual(task.unique_key, 'stored-key')

    def test_unique_key_none_by_default(self):
        """Tasks without unique_key have unique_key=False."""
        task = self.Task.create_task('test.task.type.noop')
        self.assertFalse(task.unique_key)

    # ------------------------------------------------------------------
    # Keys are independent per value
    # ------------------------------------------------------------------

    def test_different_keys_create_independent_tasks(self):
        """Different unique_key values do not interfere with each other."""
        t1 = self.Task.create_task(
            'test.task.type.noop', unique_key='key-alpha')
        t2 = self.Task.create_task(
            'test.task.type.noop', unique_key='key-beta')
        self.assertNotEqual(t1.id, t2.id)
        self.assertEqual(t1.state, 'pending')
        self.assertEqual(t2.state, 'pending')


class TestUniqueKeyConcurrentRace(TransactionCase):
    """Simulate the concurrent-insert race that the savepoint handles.

    Race scenario:
      T1: flush + SELECT ... FOR UPDATE  →  no row found (race window)
      T2: INSERT with same unique_key    →  wins; partial index entry created
      T1: INSERT inside savepoint        →  UniqueViolation from partial index
      T1: ROLLBACK TO SAVEPOINT          →  savepoint cleaned up
      T1: fallback SELECT                →  finds T2's task; applies policy

    We replicate this deterministically — no real threads needed:
      - fetchone() returns None on the first call (initial SELECT misses T2)
      - cr.savepoint() returns a CM whose __enter__ raises UniqueViolation
        (simulates T1's INSERT being blocked by T2's committed row)
      - fetchone() returns the real row on the second call (fallback SELECT)
    """

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']

    def _make_winner(self, unique_key):
        """Insert a task directly (bypasses create_task uniqueness check)."""
        task = self.Task.sudo().create({
            'name': 'Concurrent winner',
            'type_code': 'test.task.type.noop',
            'unique_key': unique_key,
        })
        self.Task.flush_model()
        return task

    def _race_patches(self):
        """Return two context managers that simulate the race window."""

        # fetchone() call 1: initial SELECT - None (race window, no T2 yet)
        # fetchone() call 2: fallback SELECT - real row (T2's task)
        original_fetchone = self.env.cr.fetchone
        calls = [0]

        def _mock_fetchone():
            calls[0] += 1
            if calls[0] == 1:
                return None
            return original_fetchone()

        # savepoint CM whose __enter__ raises UniqueViolation
        # (T1's INSERT is rejected by the partial unique index)
        class _ConflictingSavepoint:
            def __enter__(self):
                raise psycopg2.errors.UniqueViolation(
                    "simulated concurrent INSERT")

            def __exit__(self, *args):
                pass

        def _mock_savepoint(flush=False):
            return _ConflictingSavepoint()

        return (
            patch.object(
                self.env.cr, 'fetchone', side_effect=_mock_fetchone),
            patch.object(
                self.env.cr, 'savepoint', side_effect=_mock_savepoint),
        )

    def test_race_reuse_running_returns_winner(self):
        """reuse-running: fallback SELECT returns T2's task."""
        winner = self._make_winner('race-reuse-key')
        p_fetchone, p_savepoint = self._race_patches()

        with p_fetchone, p_savepoint:
            result = self.Task.create_task(
                'test.task.type.noop', unique_key='race-reuse-key')

        self.assertEqual(result.id, winner.id)

    def test_race_raise_raises_with_winner(self):
        """raise policy: fallback SELECT finds winner, raises exception."""
        winner = self._make_winner('race-raise-key')
        p_fetchone, p_savepoint = self._race_patches()

        caught = None
        with p_fetchone, p_savepoint:
            try:
                self.Task.create_task(
                    'test.task.type.noop', unique_key='race-raise-key',
                    on_conflict='raise')
            except AlreadyScheduledException as exc:
                caught = exc

        self.assertIsNotNone(caught, "Expected AlreadyScheduledException")
        self.assertEqual(caught.task.id, winner.id)
