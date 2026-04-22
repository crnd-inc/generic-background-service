from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo import exceptions
from odoo.addons.generic_task_queue.service.task_type import (
    AbstractTaskType, ChildResult)


class TestWaitingState(TransactionCase):
    """Test the 'waiting' state for parent tasks that spawn children."""

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.Worker = self.env['generic.task.queue.worker']
        self.worker = self.Worker.create({
            'uuid': 'parent-child-worker',
            'service_name': 'test.service',
            'state': 'active',
        })

    def test_running_to_waiting_transition(self):
        """running → waiting should be a valid transition."""
        task = self.Task.create_task(
            'test.task.type.noop', name='Parent')
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()
        task.sudo().action_wait_children()
        self.assertEqual(task.state, 'waiting')

    def test_waiting_to_done_transition(self):
        """waiting → done should be valid (all children done)."""
        task = self.Task.create_task(
            'test.task.type.noop', name='Parent')
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()
        task.sudo().action_wait_children()
        task.sudo().action_done({'aggregated': True})
        self.assertEqual(task.state, 'done')

    def test_waiting_to_failed_transition(self):
        """waiting → failed should be valid (child failed)."""
        task = self.Task.create_task(
            'test.task.type.noop', name='Parent')
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()
        task.sudo().action_wait_children()
        task.sudo().action_fail('Child failed')
        self.assertEqual(task.state, 'failed')

    def test_waiting_to_cancelled_transition(self):
        """waiting → cancelled should be valid."""
        task = self.Task.create_task(
            'test.task.type.noop', name='Parent')
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()
        task.sudo().action_wait_children()
        task.action_cancel()
        self.assertEqual(task.state, 'cancelled')

    def test_pending_to_waiting_invalid(self):
        """pending → waiting should NOT be valid."""
        task = self.Task.create_task(
            'test.task.type.noop', name='Bad')
        with self.assertRaises(exceptions.ValidationError):
            task.sudo().action_wait_children()


class TestCreateChildren(TransactionCase):
    """Test the create_children() convenience method."""

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']

    def test_create_children_basic(self):
        """create_children should create tasks with parent_id set."""
        parent = self.Task.create_task(
            'test.task.type.noop', name='Parent')

        children = self.Task.create_children(
            parent, 'test.task.type.noop',
            [{'key': 'val1'}, {'key': 'val2'}, {'key': 'val3'}],
        )
        self.assertEqual(len(children), 3)
        for child in children:
            self.assertEqual(child.parent_id, parent)
            self.assertEqual(child.state, 'pending')
            self.assertEqual(child.type_code, 'test.task.type.noop')
        self.assertEqual(children[0].task_params, {'key': 'val1'})

    def test_create_children_inherits_channel(self):
        """Children should inherit channel from parent by default."""
        parent = self.Task.create_task(
            'test.task.type.noop', name='Parent',
            channel='heavy')

        children = self.Task.create_children(
            parent, 'test.task.type.noop',
            [{'data': 1}],
        )
        self.assertEqual(children[0].channel, 'heavy')

    def test_create_children_custom_vals(self):
        """Common vals should be applied to all children."""
        parent = self.Task.create_task(
            'test.task.type.noop', name='Parent')

        children = self.Task.create_children(
            parent, 'test.task.type.noop',
            [{'data': 1}, {'data': 2}],
            priority=1,
            channel='custom',
        )
        for child in children:
            self.assertEqual(child.priority, 1)
            self.assertEqual(child.channel, 'custom')

    def test_create_children_names(self):
        """Children should have auto-generated names."""
        parent = self.Task.create_task(
            'test.task.type.noop', name='My Batch')

        children = self.Task.create_children(
            parent, 'test.task.type.noop',
            [{'a': 1}, {'b': 2}],
        )
        self.assertIn('My Batch', children[0].name)


class TestParentWaitsForChildren(TransactionCase):
    """Test the worker logic that completes waiting parents."""

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.Worker = self.env['generic.task.queue.worker']
        self.worker = self.Worker.create({
            'uuid': 'waiting-test-worker',
            'service_name': 'test.service',
            'state': 'active',
        })

    def _make_waiting_parent_with_children(self, n_children=3):
        """Helper: create a waiting parent with n pending children."""
        parent = self.Task.create_task(
            'test.task.type.noop', name='Waiting Parent')
        parent.sudo().action_assign(self.worker)
        parent.sudo().action_start()

        children = self.Task.create_children(
            parent, 'test.task.type.noop',
            [{'idx': i} for i in range(n_children)],
        )
        parent.sudo().action_wait_children()
        return parent, children

    def test_parent_stays_waiting_while_children_pending(self):
        """Parent should stay in 'waiting' while children
        are still pending/running."""
        parent, children = self._make_waiting_parent_with_children(3)

        # Complete only 2 of 3
        for child in children[:2]:
            child.sudo().action_assign(self.worker)
            child.sudo().action_start()
            child.sudo().action_done({'ok': True})

        # Parent should still be waiting
        self.assertEqual(parent.state, 'waiting')

    def test_parent_completes_when_all_children_done(self):
        """Parent should transition to done when ALL children
        are done."""
        parent, children = self._make_waiting_parent_with_children(2)

        for child in children:
            child.sudo().action_assign(self.worker)
            child.sudo().action_start()
            child.sudo().action_done({'ok': True})

        # Check if parent should complete
        # (this is what the worker would call)
        parent.sudo()._check_waiting_parent()

        self.assertEqual(parent.state, 'done')

    def test_parent_fails_when_child_fails_permanently(self):
        """If a child fails and can't retry, parent should fail."""
        parent, children = self._make_waiting_parent_with_children(2)

        # Child 0 done
        children[0].sudo().action_assign(self.worker)
        children[0].sudo().action_start()
        children[0].sudo().action_done({'ok': True})

        # Child 1 fails with no retries
        children[1].retry_policy = 'non_retriable'
        children[1].sudo().action_assign(self.worker)
        children[1].sudo().action_start()
        children[1].sudo().action_fail('permanent error')

        parent.sudo()._check_waiting_parent()
        self.assertEqual(parent.state, 'failed')

    def test_parent_stays_waiting_when_retriable_child_has_retries(self):
        """Parent stays 'waiting' when a retriable child failed
        but still has retry attempts remaining (retry_count < max_retries)."""
        parent, children = self._make_waiting_parent_with_children(2)

        children[0].sudo().action_assign(self.worker)
        children[0].sudo().action_start()
        children[0].sudo().action_done({'ok': True})

        # Child 1 is retriable with retries left
        children[1].sudo().write({
            'retry_policy': 'retriable',
            'max_retries': 3,
            'retry_count': 0,
        })
        children[1].sudo().action_assign(self.worker)
        children[1].sudo().action_start()
        children[1].sudo().action_fail('transient error')
        # retry_count=0 < max_retries=3 → still retriable, parent should wait

        parent.sudo()._check_waiting_parent()
        self.assertEqual(parent.state, 'waiting')

    def test_parent_fails_when_retriable_child_exhausts_retries(self):
        """Parent fails when a retriable child has used all retry attempts
        (retry_count >= max_retries)."""
        parent, children = self._make_waiting_parent_with_children(2)

        children[0].sudo().action_assign(self.worker)
        children[0].sudo().action_start()
        children[0].sudo().action_done({'ok': True})

        # Child 1 is retriable but retries are exhausted
        children[1].sudo().write({
            'retry_policy': 'retriable',
            'max_retries': 2,
            'retry_count': 2,
        })
        children[1].sudo().action_assign(self.worker)
        children[1].sudo().action_start()
        children[1].sudo().action_fail('still failing after retries')
        # retry_count=2 == max_retries=2 → exhausted → parent must fail

        parent.sudo()._check_waiting_parent()
        self.assertEqual(parent.state, 'failed')


class TestTaskErrorData(TransactionCase):
    """Test the task_error_data JSON field."""

    def test_error_data_field_exists(self):
        """task_error_data should be a Json field."""
        Task = self.env['generic.task.queue.task']
        task = Task.create_task(
            'test.task.type.noop', name='Error data test')
        # Should be empty by default
        self.assertFalse(task.task_error_data)

    def test_error_data_stores_structured_errors(self):
        """task_error_data should store structured error info."""
        Task = self.env['generic.task.queue.task']
        Worker = self.env['generic.task.queue.worker']
        worker = Worker.create({
            'uuid': 'err-data-worker',
            'service_name': 'test.service',
            'state': 'active',
        })
        task = Task.create_task(
            'test.task.type.noop', name='Structured error')
        task.sudo().action_assign(worker)
        task.sudo().action_start()

        error_data = {
            'errors': [
                {'record_id': 1, 'msg': 'not found'},
                {'record_id': 2, 'msg': 'access denied'},
            ],
        }
        task.sudo().action_fail('2 errors occurred',
                                error_data=error_data)
        self.assertEqual(task.task_error_data, error_data)
        self.assertEqual(task.task_error, '2 errors occurred')


class TestTrackProgress(TransactionCase):
    """Test the _track_progress flag on AbstractTaskType.

    update_progress() is patched throughout: it opens a second DB cursor
    that would deadlock against the row lock held by the test transaction.
    We test the *value* passed to update_progress, not the DB side-effect.
    """

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.worker = self.env['generic.task.queue.worker'].create({
            'uuid': 'track-progress-worker',
            'service_name': 'test.service',
            'state': 'active',
        })

    def _make_task_type(self, track_progress):
        class _TrackType(AbstractTaskType):
            _name = None  # not registered — unit test only
            _track_progress = track_progress

            def execute(self, env, task):
                return {}

        return _TrackType()

    def _make_waiting_parent(self, n_children):
        parent = self.Task.create_task(
            'test.task.type.noop', name='Progress Parent')
        parent.sudo().action_assign(self.worker)
        parent.sudo().action_start()
        children = self.Task.create_children(
            parent, 'test.task.type.noop',
            [{'idx': i} for i in range(n_children)],
        )
        parent.sudo().action_wait_children()
        return parent, children

    def test_track_progress_advances_on_each_child(self):
        """update_progress should be called with increasing value per child."""
        parent, children = self._make_waiting_parent(4)
        task_type = self._make_task_type(track_progress=True)

        for i, child in enumerate(children):
            child.sudo().action_assign(self.worker)
            child.sudo().action_start()
            child.sudo().action_done({})
            with patch.object(
                parent.__class__, 'update_progress'
            ) as mock_update:
                task_type.on_child_done(self.env, parent, child)
            expected = int((i + 1) * 100 / 4)
            mock_update.assert_called_once_with(expected)

    def test_track_progress_false_skips_update(self):
        """on_child_done should not call update_progress when flag is False."""
        parent, children = self._make_waiting_parent(2)
        task_type = self._make_task_type(track_progress=False)

        child = children[0]
        child.sudo().action_assign(self.worker)
        child.sudo().action_start()
        child.sudo().action_done({})
        with patch.object(
            parent.__class__, 'update_progress'
        ) as mock_update:
            task_type.on_child_done(self.env, parent, child)
        mock_update.assert_not_called()

    def test_track_progress_counts_failed_children(self):
        """Failed children count toward progress (batch has advanced)."""
        parent, children = self._make_waiting_parent(2)
        task_type = self._make_task_type(track_progress=True)

        child = children[0]
        child.sudo().action_assign(self.worker)
        child.sudo().action_start()
        child.sudo().action_fail('boom')
        with patch.object(
            parent.__class__, 'update_progress'
        ) as mock_update:
            task_type.on_child_done(self.env, parent, child)
        mock_update.assert_called_once_with(50)

    def test_batch_parent_uses_track_progress(self):
        """TestTaskTypeBatchParent uses _track_progress flag."""
        from ..service.test_task_types import TestTaskTypeBatchParent
        self.assertTrue(TestTaskTypeBatchParent._track_progress)


class TestIterChildResults(TransactionCase):
    """Test AbstractTaskType.iter_child_results()."""

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.worker = self.env['generic.task.queue.worker'].create({
            'uuid': 'iter-results-worker',
            'service_name': 'test.service',
            'state': 'active',
        })

        class _SimpleType(AbstractTaskType):
            _name = None

            def execute(self, env, task):
                return {}

        self.task_type = _SimpleType()

    def _make_children(self, states_and_results):
        """Create a waiting parent whose children are in given states.

        states_and_results: list of
            ('done'|'failed'|'cancelled', result_or_error)
        """
        parent = self.Task.create_task(
            'test.task.type.noop', name='Iter Parent')
        parent.sudo().action_assign(self.worker)
        parent.sudo().action_start()
        children = self.Task.create_children(
            parent, 'test.task.type.noop',
            [{'idx': i} for i in range(len(states_and_results))],
        )
        parent.sudo().action_wait_children()

        for child, (state, payload) in zip(children, states_and_results):
            child.sudo().action_assign(self.worker)
            child.sudo().action_start()
            if state == 'done':
                child.sudo().action_done(payload)
            elif state == 'failed':
                child.sudo().action_fail(payload)
            elif state == 'cancelled':
                child.action_cancel()

        return parent

    def test_iter_yields_child_result_namedtuples(self):
        """iter_child_results should yield ChildResult instances."""
        parent = self._make_children([('done', {'x': 1})])
        results = list(self.task_type.iter_child_results(parent))
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], ChildResult)

    def test_done_child_has_result_and_no_error(self):
        parent = self._make_children([('done', {'value': 42})])
        cr = list(self.task_type.iter_child_results(parent))[0]
        self.assertEqual(cr.state, 'done')
        self.assertEqual(cr.result, {'value': 42})
        self.assertIsNone(cr.error)

    def test_failed_child_has_error_and_no_result(self):
        parent = self._make_children([('failed', 'something broke')])
        cr = list(self.task_type.iter_child_results(parent))[0]
        self.assertEqual(cr.state, 'failed')
        self.assertIsNone(cr.result)
        self.assertEqual(cr.error, 'something broke')

    def test_cancelled_child_has_neither(self):
        parent = self._make_children([('cancelled', None)])
        cr = list(self.task_type.iter_child_results(parent))[0]
        self.assertEqual(cr.state, 'cancelled')
        self.assertIsNone(cr.result)
        self.assertIsNone(cr.error)

    def test_mixed_children(self):
        """iter_child_results should handle mixed terminal states."""
        parent = self._make_children([
            ('done', {'ids': [1, 2]}),
            ('failed', 'timeout'),
            ('cancelled', None),
        ])
        results = list(self.task_type.iter_child_results(parent))
        self.assertEqual(len(results), 3)
        states = [cr.state for cr in results]
        self.assertIn('done', states)
        self.assertIn('failed', states)
        self.assertIn('cancelled', states)

    def test_task_type_field_points_to_record(self):
        """ChildResult.task should be the child task record."""
        parent = self._make_children([('done', {})])
        cr = list(self.task_type.iter_child_results(parent))[0]
        self.assertEqual(cr.task._name, 'generic.task.queue.task')
        self.assertEqual(cr.task.parent_id, parent)


class TestPropagateProgress(TransactionCase):
    """Test the _propagate_progress flag on AbstractTaskType.

    _propagate_progress_upward is patched throughout: it opens a second
    DB cursor that cannot see uncommitted test rows.  We verify that
    update_progress() invokes it with the correct parent_id, and trust
    the SQL logic via code review + pre-commit checks.
    """

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']

    def _make_parent_and_child(self, child_type_code):
        parent = self.Task.create_task(
            'test.task.type.noop', name='Root')
        child = self.Task.create_children(
            parent, child_type_code, [{}])[0]
        return parent, child

    def test_propagate_progress_default_false(self):
        """_propagate_progress should default to False on AbstractTaskType."""
        self.assertFalse(AbstractTaskType._propagate_progress)

    def test_propagating_child_type_has_flag_true(self):
        """TestTaskTypePropagatingChild has _propagate_progress=True."""
        from ..service.test_task_types import (
            TestTaskTypePropagatingChild)
        self.assertTrue(TestTaskTypePropagatingChild._propagate_progress)

    def test_propagating_child_type_synced_to_db(self):
        """propagate_progress flag should be synced to the DB type record."""
        tt = self.env['generic.task.queue.task.type'].search([
            ('code', '=', 'test.task.type.propagating.child'),
        ], limit=1)
        self.assertTrue(tt.propagate_progress)

    def test_non_propagating_type_synced_to_db(self):
        """Non-propagating types have propagate_progress=False in DB."""
        tt = self.env['generic.task.queue.task.type'].search([
            ('code', '=', 'test.task.type.noop'),
        ], limit=1)
        self.assertFalse(tt.propagate_progress)

    def test_propagate_calls_upward_when_flag_set(self):
        """update_progress should call _propagate_progress_upward
        when the task type has propagate_progress=True."""
        parent, child = self._make_parent_and_child(
            'test.task.type.propagating.child')

        with patch.object(
            child.__class__, '_propagate_progress_upward'
        ) as mock_propagate:
            child.update_progress(50)

        mock_propagate.assert_called_once()
        # Third positional arg is parent_id
        call_args = mock_propagate.call_args[0]
        self.assertEqual(call_args[-1], parent.id)

    def test_propagate_not_called_when_flag_false(self):
        """update_progress should NOT call _propagate_progress_upward
        for non-propagating task types."""
        parent, child = self._make_parent_and_child('test.task.type.noop')

        with patch.object(
            child.__class__, '_propagate_progress_upward'
        ) as mock_propagate:
            child.update_progress(50)

        mock_propagate.assert_not_called()

    def test_propagate_not_called_without_parent(self):
        """update_progress should NOT propagate for root tasks
        (no parent_id) even if type has the flag."""
        root = self.Task.create_task(
            'test.task.type.propagating.child', name='Root')

        with patch.object(
            root.__class__, '_propagate_progress_upward'
        ) as mock_propagate:
            root.update_progress(50)

        mock_propagate.assert_not_called()
