from odoo.tests.common import TransactionCase
from odoo import exceptions


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
