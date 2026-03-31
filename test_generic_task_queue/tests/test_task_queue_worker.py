from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_type_registry import (
    TaskTypeRegistry,
)


class TestWorkerModel(TransactionCase):
    """Test the generic.task.queue.worker model."""

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
            'state': 'stale',
        })
        w.heartbeat()
        self.assertEqual(w.state, 'active')
        self.assertTrue(w.last_heartbeat)

    def test_mark_dead_reassigns_retriable(self):
        """mark_dead() should put retriable tasks back to pending."""
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
            'retry_policy': 'retriable',
        })
        task.action_assign(w)
        task.action_start()
        self.assertEqual(task.state, 'running')

        w.mark_dead()
        self.assertEqual(w.state, 'dead')
        self.assertEqual(task.state, 'pending')
        self.assertFalse(task.worker_id)

    def test_mark_dead_fails_non_retriable(self):
        """mark_dead() should fail non-retriable tasks."""
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
            'retry_policy': 'non_retriable',
        })
        task.action_assign(w)
        task.action_start()

        w.mark_dead()
        self.assertEqual(task.state, 'failed')
        self.assertIn('Worker died', task.task_error)

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
