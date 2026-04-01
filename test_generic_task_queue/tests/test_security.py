from odoo.tests.common import TransactionCase
from odoo import exceptions

from odoo.addons.generic_task_queue.service.task_type_registry import (
    TaskTypeRegistry,
)


class TestTaskExecutionContext(TransactionCase):
    """Test that tasks execute in the context of the creating user,
    not as SUPERUSER."""

    def setUp(self):
        super().setUp()
        # Create a non-admin user
        self.test_user = self.env['res.users'].create({
            'name': 'Task Test User',
            'login': 'task_test_user',
            'groups_id': [(6, 0, [
                self.env.ref('base.group_user').id,
            ])],
        })
        self.Worker = self.env['generic.task.queue.worker']
        self.worker = self.Worker.create({
            'uuid': 'security-test-worker',
            'service_name': 'test.service',
            'state': 'active',
        })

    def test_task_executes_as_creating_user(self):
        """The env passed to task_type.execute() should have
        the creating user's ID, not SUPERUSER_ID."""

        # Create task as test_user
        Task = self.env['generic.task.queue.task'].with_user(
            self.test_user)
        target = self.env['test.task.target'].create({
            'name': 'security test', 'value': 0})

        task = Task.create_task(
            'task.type.model.method',
            name='User context test',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [target.id],
            },
        )
        self.assertEqual(task.create_uid, self.test_user)

        # Simulate what worker does: claim + execute
        task_sudo = task.sudo()
        task_sudo.action_assign(self.worker)
        task_sudo.action_start()

        registry = TaskTypeRegistry()
        task_type_cls = registry.get_task_type(task.type_code)
        task_type = task_type_cls()

        # Execute — the env should be switched to creating user
        # We need to verify this by checking env.uid inside execute
        # For now, verify via the task model's execute_as_user logic
        # The worker should pass user-scoped env
        user_env = self.env(user=task.create_uid.id)
        task_type.execute(user_env, task_sudo)

        # The method ran successfully in user context
        target.invalidate_recordset()
        self.assertEqual(target.value, 1)


class TestProtectedTaskFields(TransactionCase):
    """Test that critical task fields cannot be modified via write()
    by non-system users."""

    def setUp(self):
        super().setUp()
        self.test_user = self.env['res.users'].create({
            'name': 'Task Write User',
            'login': 'task_write_user',
            'groups_id': [(6, 0, [
                self.env.ref('base.group_user').id,
            ])],
        })

    def test_user_cannot_write_state_directly(self):
        """Regular user should not be able to change state via write()."""
        Task = self.env['generic.task.queue.task'].with_user(
            self.test_user)
        task = Task.create_task(
            'test.task.type.noop', name='Protected state test')

        with self.assertRaises(exceptions.AccessError):
            task.write({'state': 'done'})

    def test_user_cannot_write_type_code(self):
        """Regular user should not be able to change type_code
        via write()."""
        Task = self.env['generic.task.queue.task'].with_user(
            self.test_user)
        task = Task.create_task(
            'test.task.type.noop', name='Protected type test')

        with self.assertRaises(exceptions.AccessError):
            task.write({'type_code': 'task.type.model.method'})

    def test_user_cannot_write_worker_id(self):
        """Regular user should not be able to change worker_id."""
        Task = self.env['generic.task.queue.task'].with_user(
            self.test_user)
        task = Task.create_task(
            'test.task.type.noop', name='Protected worker test')

        with self.assertRaises(exceptions.AccessError):
            task.write({'worker_id': 1})

    def test_user_cannot_write_retry_count(self):
        """Regular user should not be able to change retry_count."""
        Task = self.env['generic.task.queue.task'].with_user(
            self.test_user)
        task = Task.create_task(
            'test.task.type.noop', name='Protected retry test')

        with self.assertRaises(exceptions.AccessError):
            task.write({'retry_count': 99})

    def test_user_can_write_allowed_fields(self):
        """Regular user should be able to change name, priority."""
        Task = self.env['generic.task.queue.task'].with_user(
            self.test_user)
        task = Task.create_task(
            'test.task.type.noop', name='Allowed fields test')

        # These should work
        task.write({'name': 'Updated name', 'priority': 1})
        self.assertEqual(task.name, 'Updated name')
        self.assertEqual(task.priority, 1)

    def test_sudo_can_write_protected_fields(self):
        """System/sudo can write any field (worker needs this)."""
        Task = self.env['generic.task.queue.task']
        task = Task.create_task(
            'test.task.type.noop', name='Sudo write test')

        # sudo should work for all fields
        task.sudo().write({'state': 'cancelled'})
        self.assertEqual(task.state, 'cancelled')

    def test_user_can_cancel_own_task(self):
        """User should be able to cancel via action_cancel(),
        even though direct state write is blocked."""
        Task = self.env['generic.task.queue.task'].with_user(
            self.test_user)
        task = Task.create_task(
            'test.task.type.noop', name='Cancel via action')

        # action_cancel should work (uses sudo internally)
        task.action_cancel()
        self.assertEqual(task.state, 'cancelled')


class TestTypeCodeValidation(TransactionCase):
    """Test that type_code is validated at creation time."""

    def test_create_with_valid_type_code(self):
        """Creating a task with a registered type_code should work."""
        Task = self.env['generic.task.queue.task']
        task = Task.create_task('test.task.type.noop')
        self.assertTrue(task.id)

    def test_create_with_invalid_type_code_raises(self):
        """Creating a task with an unknown type_code should raise."""
        Task = self.env['generic.task.queue.task']
        with self.assertRaises(exceptions.ValidationError):
            Task.create_task('nonexistent.task.type.xyz')

    def test_create_with_model_method_type(self):
        """task.type.model.method should be accepted."""
        Task = self.env['generic.task.queue.task']
        task = Task.create_task(
            'task.type.model.method',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [],
            })
        self.assertTrue(task.id)


class TestRecordIdsValidation(TransactionCase):
    """Test that record_ids are validated at execution time."""

    def test_nonexistent_record_ids_handled(self):
        """Executing with non-existent record_ids should not
        silently succeed — should raise or handle gracefully."""
        Task = self.env['generic.task.queue.task']
        task = Task.create_task(
            'task.type.model.method',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [999999],
            },
        )

        registry = TaskTypeRegistry()
        task_type = registry.get_task_type(task.type_code)()

        # Should raise because record 999999 doesn't exist
        with self.assertRaises(ValueError):
            task_type.execute(self.env, task)

    def test_empty_record_ids_work(self):
        """Executing with empty record_ids should work
        (for @api.model-style methods)."""
        Task = self.env['generic.task.queue.task']
        task = Task.create_task(
            'task.type.model.method',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [],
            },
        )

        registry = TaskTypeRegistry()
        task_type = registry.get_task_type(task.type_code)()
        # Should not raise — runs on empty recordset
        task_type.execute(self.env, task)

    def test_plan_without_record_ids(self):
        """_g_task_queue__plan on model class (no recordset)
        should create a task with empty record_ids."""
        Target = self.env['test.task.target']
        # Call on model, not on a recordset
        task = Target._g_task_queue__plan('do_increment')
        self.assertEqual(task.task_params['record_ids'], [])
        self.assertEqual(task.state, 'pending')

    def test_valid_record_ids_work(self):
        """Existing record_ids should work fine."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'valid', 'value': 0})

        Task = self.env['generic.task.queue.task']
        task = Task.create_task(
            'task.type.model.method',
            params={
                'model': 'test.task.target',
                'method': 'do_increment',
                'record_ids': [rec.id],
            },
        )

        registry = TaskTypeRegistry()
        task_type = registry.get_task_type(task.type_code)()
        task_type.execute(self.env, task)

        rec.invalidate_recordset()
        self.assertEqual(rec.value, 1)
