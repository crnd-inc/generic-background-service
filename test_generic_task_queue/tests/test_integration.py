from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_type_registry import (
    TaskTypeRegistry,
)


class TestCreateTaskConvenience(TransactionCase):
    """Test the create_task() convenience method."""

    def test_create_task_minimal(self):
        """create_task with only type_code should work."""
        Task = self.env['generic.task.queue.task']
        task = Task.create_task('test.task.type.noop')
        self.assertEqual(task.type_code, 'test.task.type.noop')
        self.assertEqual(task.name, 'test.task.type.noop')
        self.assertEqual(task.state, 'pending')
        self.assertEqual(task.channel, 'default')
        self.assertEqual(task.priority, 5)

    def test_create_task_full(self):
        """create_task with all params."""
        Task = self.env['generic.task.queue.task']
        task = Task.create_task(
            'task.type.model.method',
            name='Increment values',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [1],
            },
            channel='heavy',
            priority=1,
            timeout=300,
            retry_policy='non_retriable',
            max_retries=0,
        )
        self.assertEqual(task.name, 'Increment values')
        self.assertEqual(task.channel, 'heavy')
        self.assertEqual(task.priority, 1)
        self.assertEqual(task.timeout, 300)
        self.assertEqual(task.retry_policy, 'non_retriable')
        self.assertEqual(
            task.task_params['model'], 'test.task.target')

    def test_create_task_with_parent(self):
        """create_task should support parent_id for sub-tasks."""
        Task = self.env['generic.task.queue.task']
        parent = Task.create_task('test.task.type.noop',
                                  name='Parent')
        child = Task.create_task('test.task.type.noop',
                                 name='Child',
                                 parent_id=parent.id)
        self.assertEqual(child.parent_id, parent)
        self.assertIn(child, parent.child_ids)


class TestEndToEndExecution(TransactionCase):
    """End-to-end test: create task → execute → verify result.

    This tests the full flow without running the actual
    BackgroundService worker thread. Instead, we simulate
    what the worker does: claim → start → execute → done.
    """

    def test_model_method_task_e2e(self):
        """Create a ModelMethodTaskType task, execute it manually,
        verify the target record was processed."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'e2e', 'value': 10})

        Task = self.env['generic.task.queue.task']
        task = Task.create_task(
            'task.type.model.method',
            name='E2E increment',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [rec.id],
                'kwargs': {'amount': 5},
            },
        )
        self.assertEqual(task.state, 'pending')

        # Simulate worker: claim
        Worker = self.env['generic.task.queue.worker']
        worker = Worker.create({
            'uuid': 'e2e-worker',
            'service_name': 'test.service',
            'state': 'active',
        })
        claimed = Task.claim_task(
            worker, ['default'], ['task.type.model.method'])
        self.assertEqual(claimed, task)
        self.assertEqual(task.state, 'assigned')

        # Simulate worker: start
        task.action_start()
        self.assertEqual(task.state, 'running')

        # Simulate worker: execute
        registry = TaskTypeRegistry()
        task_type_cls = registry.get_task_type(task.type_code)
        task_type = task_type_cls()
        result = task_type.execute(self.env, task)

        # Simulate worker: done
        task.action_done(result)
        self.assertEqual(task.state, 'done')
        self.assertTrue(task.date_completed)
        self.assertEqual(task.progress, 100)

        # Verify the actual work was done
        rec.invalidate_recordset()
        self.assertEqual(rec.value, 15)
        self.assertTrue(rec.processed)

    def test_failed_task_auto_retry_flow(self):
        """Simulate: task fails → action_retry → re-claim → succeed."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'retry-test', 'value': 0})

        Task = self.env['generic.task.queue.task']
        task = Task.create_task(
            'task.type.model.method',
            name='Will fail then succeed',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [rec.id],
            },
            max_retries=3,
        )

        Worker = self.env['generic.task.queue.worker']
        worker = Worker.create({
            'uuid': 'retry-worker',
            'service_name': 'test.service',
            'state': 'active',
        })

        # First attempt: claim → start → fail
        Task.claim_task(worker, ['default'],
                        ['task.type.model.method'])
        task.action_start()
        task.action_fail('Simulated error')
        self.assertEqual(task.state, 'failed')
        self.assertEqual(task.retry_count, 1)

        # Auto-retry puts it back to pending
        task.action_retry()
        self.assertEqual(task.state, 'pending')

        # Second attempt: claim → start → succeed
        Task.claim_task(worker, ['default'],
                        ['task.type.model.method'])
        task.action_start()

        registry = TaskTypeRegistry()
        task_type = registry.get_task_type(task.type_code)()
        result = task_type.execute(self.env, task)
        task.action_done(result)

        self.assertEqual(task.state, 'done')
        rec.invalidate_recordset()
        self.assertEqual(rec.value, 1)


class TestAutoRetry(TransactionCase):
    """Test the auto-retry logic that runs in the worker's poll loop.

    We test the _auto_retry_failed search logic directly
    against the ORM, since the bug is in the search domain.
    """

    def test_auto_retry_search_does_not_crash(self):
        """The auto-retry search should not crash.

        Bug: the search domain used Task._fields['max_retries'].default
        which could be a function reference, causing TypeError.
        """
        Task = self.env['generic.task.queue.task']
        Worker = self.env['generic.task.queue.worker']
        worker = Worker.create({
            'uuid': 'auto-retry-test',
            'service_name': 'test.service',
            'state': 'active',
        })

        # Create a failed retriable task
        task = Task.create_task(
            'test.task.type.noop',
            name='Auto-retry test',
            max_retries=3,
        )
        task.action_assign(worker)
        task.action_start()
        task.action_fail('test error')
        self.assertEqual(task.state, 'failed')
        self.assertEqual(task.retry_count, 1)

        # Simulate what the worker's _auto_retry_failed does:
        # search for failed retriable tasks
        failed = Task.search([
            ('state', '=', 'failed'),
            ('retry_policy', '=', 'retriable'),
            ('channel', 'in', ['default']),
            ('type_code', 'in', ['test.task.type.noop']),
        ], limit=10)
        self.assertIn(task, failed)

        # SQL already filters retry_count <= max_retries
        for t in failed:
            t.action_retry()

        self.assertEqual(task.state, 'pending')


class TestPlanTaskShortcut(TransactionCase):
    """Test the _g_task_queue__plan() shortcut on base model."""

    def test_plan_task_from_recordset(self):
        """_g_task_queue__plan() should create a task
        using self.ids as record_ids."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'plan-test', 'value': 0})

        task = rec._g_task_queue__plan('do_increment', amount=3)

        self.assertEqual(task.state, 'pending')
        self.assertEqual(task.type_code, 'task.type.model.method')
        self.assertEqual(task.task_params['model'], 'test.task.target')
        self.assertEqual(task.task_params['method'], 'do_increment')
        self.assertEqual(task.task_params['record_ids'], [rec.id])
        self.assertEqual(task.task_params['kwargs'], {'amount': 3})

    def test_plan_task_auto_name(self):
        """Auto-generated name should include model, method, and IDs."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'name-test', 'value': 0})

        task = rec._g_task_queue__plan('do_increment')

        self.assertIn('test.task.target', task.name)
        self.assertIn('do_increment', task.name)
        self.assertIn(str(rec.id), task.name)

    def test_plan_task_custom_name(self):
        """Explicit name should be used."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'custom', 'value': 0})

        task = rec._g_task_queue__plan(
            'do_increment', name='My custom task')

        self.assertEqual(task.name, 'My custom task')

    def test_plan_task_explicit_record_ids(self):
        """record_ids param should override self.ids."""
        Target = self.env['test.task.target']
        task = Target._g_task_queue__plan(
            'do_increment', record_ids=[99, 100])

        self.assertEqual(task.task_params['record_ids'], [99, 100])

    def test_plan_task_no_kwargs(self):
        """When no kwargs, params should not have 'kwargs' key."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'no-kw', 'value': 0})

        task = rec._g_task_queue__plan('do_increment')

        self.assertNotIn('kwargs', task.task_params)

    def test_plan_task_with_channel_and_timeout(self):
        """Channel, priority, timeout should be passed through."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'opts', 'value': 0})

        task = rec._g_task_queue__plan(
            'do_increment',
            channel='heavy',
            priority=1,
            timeout=600,
        )

        self.assertEqual(task.channel, 'heavy')
        self.assertEqual(task.priority, 1)
        self.assertEqual(task.timeout, 600)

    def test_plan_task_available_on_any_model(self):
        """_g_task_queue__plan should be available on any model."""
        task = self.env['res.partner']._g_task_queue__plan(
            'write', record_ids=[])

        self.assertEqual(task.task_params['model'], 'res.partner')
        self.assertEqual(task.task_params['method'], 'write')
