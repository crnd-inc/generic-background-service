from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_type_registry import (
    TaskTypeRegistry,
)


class TestWorkerProcessesTask(TransactionCase):
    """Integration test: simulate the worker's claim-execute-done
    flow synchronously."""

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.Worker = self.env['generic.task.queue.worker']
        self.worker = self.Worker.create({
            'uuid': 'integ-worker',
            'service_name': 'test.service',
            'state': 'active',
        })
        self.task_type_registry = TaskTypeRegistry()

    def _execute_task(self, task):
        """Simulate what the worker does: claim → start → execute
        → done/fail. All synchronous, no threads."""
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()

        task_type_cls = self.task_type_registry.get_task_type(task.type_code)
        task_type = task_type_cls()
        try:
            result = task_type.execute(self.env, task)
            task.sudo().action_done(result)
        except Exception:
            import traceback
            task.sudo().action_fail(traceback.format_exc())

    def test_noop_task_e2e(self):
        """Full flow: create → claim → execute → done."""
        task = self.Task.create_task('test.task.type.noop',
                                     name='E2E noop')
        self._execute_task(task)

        self.assertEqual(task.state, 'done')
        self.assertEqual(task.task_result, {'status': 'noop'})
        self.assertEqual(task.progress, 100)
        self.assertTrue(task.date_started)
        self.assertTrue(task.date_completed)

    def test_echo_task_e2e(self):
        """Echo task returns its params as result."""
        task = self.Task.create_task(
            'test.task.type.echo',
            name='E2E echo',
            params={'hello': 'world'},
        )
        self._execute_task(task)

        self.assertEqual(task.state, 'done')
        self.assertEqual(task.task_result, {'hello': 'world'})

    def test_model_method_task_e2e(self):
        """Model method task calls the decorated method."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'integ', 'value': 5})

        task = self.Task.create_task(
            'task.type.model.method',
            name='E2E model method',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [rec.id],
                'kwargs': {'amount': 10},
            },
        )
        self._execute_task(task)

        self.assertEqual(task.state, 'done')
        rec.invalidate_recordset()
        self.assertEqual(rec.value, 15)

    def test_failed_task_e2e(self):
        """Task that fails should have error recorded."""
        task = self.Task.create_task(
            'task.type.model.method',
            name='E2E failure',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [999999],
            },
        )
        self._execute_task(task)

        self.assertEqual(task.state, 'failed')
        self.assertTrue(task.task_error)
        self.assertEqual(task.retry_count, 1)


class TestBatchParentChildE2E(TransactionCase):
    """End-to-end test of the parent/child batch pattern.

    Simulates:
    1. Parent task splits items into chunks (children)
    2. Each child processes its chunk
    3. Parent aggregates results when all children done
    """

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.Worker = self.env['generic.task.queue.worker']
        self.worker = self.Worker.create({
            'uuid': 'batch-e2e-worker',
            'service_name': 'test.service',
            'state': 'active',
        })
        self.task_type_registry = TaskTypeRegistry()

    def _execute_task(self, task):
        """Execute a task synchronously."""
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()

        task_type_cls = self.task_type_registry.get_task_type(task.type_code)
        task_type = task_type_cls()
        try:
            result = task_type.execute(self.env, task)
            # If execute called action_wait_children, task is
            # now in 'waiting' — don't call action_done
            if task.state != 'waiting':
                task.sudo().action_done(result)
        except Exception:
            import traceback
            task.sudo().action_fail(traceback.format_exc())

    def test_batch_full_flow(self):
        """Parent spawns children → children execute →
        parent aggregates results."""
        # 1. Create parent with 5 items (chunk_size=2 → 3 children)
        parent = self.Task.create_task(
            'test.task.type.batch.parent',
            name='Batch test',
            params={'items': [1, 2, 3, 4, 5]},
        )

        # 2. Execute parent — should create children and wait
        self._execute_task(parent)
        self.assertEqual(parent.state, 'waiting')
        self.assertEqual(len(parent.child_ids), 3)

        # Children should be pending
        for child in parent.child_ids:
            self.assertEqual(child.state, 'pending')

        # 3. Execute each child
        for child in parent.child_ids:
            self._execute_task(child)
            self.assertEqual(child.state, 'done')

        # 4. Check waiting parent — should complete
        parent.sudo()._check_waiting_parent()
        self.assertEqual(parent.state, 'done')

        # 5. Verify aggregated result
        result = parent.task_result
        self.assertEqual(result['total_processed'], 5)
        # Items [1,2,3,4,5] doubled = [2,4,6,8,10]
        self.assertEqual(sorted(result['items']), [2, 4, 6, 8, 10])

    def test_batch_child_failure_fails_parent(self):
        """If a non-retriable child fails, parent should fail."""
        parent = self.Task.create_task(
            'test.task.type.batch.parent',
            name='Batch fail test',
            params={'items': [1, 2, 3, 4]},
        )
        self._execute_task(parent)
        self.assertEqual(parent.state, 'waiting')

        children = parent.child_ids.sorted('id')

        # First child succeeds
        self._execute_task(children[0])
        self.assertEqual(children[0].state, 'done')

        # Second child fails permanently
        children[1].retry_policy = 'non_retriable'
        children[1].sudo().action_assign(self.worker)
        children[1].sudo().action_start()
        children[1].sudo().action_fail('chunk error')

        # Check parent — should fail
        parent.sudo()._check_waiting_parent()
        self.assertEqual(parent.state, 'failed')
        self.assertTrue(parent.task_error)

    def test_batch_cancel_parent_cancels_children(self):
        """Cancelling a waiting parent should cancel its children."""
        parent = self.Task.create_task(
            'test.task.type.batch.parent',
            name='Batch cancel test',
            params={'items': [1, 2]},
        )
        self._execute_task(parent)
        self.assertEqual(parent.state, 'waiting')

        # Cancel parent
        parent.action_cancel()
        self.assertEqual(parent.state, 'cancelled')

        # All children should be cancelled
        for child in parent.child_ids:
            self.assertEqual(child.state, 'cancelled')

    def test_batch_children_inherit_channel(self):
        """Children should inherit channel from parent."""
        parent = self.Task.create_task(
            'test.task.type.batch.parent',
            name='Channel inherit',
            params={'items': [1, 2]},
            channel='heavy',
        )
        self._execute_task(parent)

        for child in parent.child_ids:
            self.assertEqual(child.channel, 'heavy')
