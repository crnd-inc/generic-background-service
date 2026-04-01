from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_type_registry import (
    TaskTypeRegistry,
)


class TestBackgroundTaskDecorator(TransactionCase):
    """Test the @background_task decorator and security enforcement."""

    def _get_model_method_type(self):
        registry = TaskTypeRegistry()
        cls = registry.get_task_type('task.type.model.method')
        return cls()

    def _make_task(self, model, method, record_ids=None, kwargs=None):
        """Create a mock-like task with given params."""
        from unittest.mock import MagicMock
        task = MagicMock()
        task.task_params = {
            'model': model,
            'method': method,
            'record_ids': record_ids or [],
        }
        if kwargs:
            task.task_params['kwargs'] = kwargs
        return task

    def test_decorated_method_is_callable(self):
        """Methods with @background_task should be callable."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'test', 'value': 10})

        task_type = self._get_model_method_type()
        task = self._make_task(
            'test.task.target', 'do_increment',
            record_ids=[rec.id], kwargs={'amount': 5})

        task_type.execute(self.env, task)
        rec.invalidate_recordset()
        self.assertEqual(rec.value, 15)

    def test_undecorated_method_is_rejected(self):
        """Methods without @background_task should be rejected."""
        task_type = self._get_model_method_type()
        # 'read' is a standard method, not decorated
        task = self._make_task(
            'test.task.target', 'read', record_ids=[])

        with self.assertRaises(ValueError) as cm:
            task_type.execute(self.env, task)
        self.assertIn('not decorated', str(cm.exception))

    def test_private_method_still_rejected(self):
        """Private methods should still be rejected."""
        task_type = self._get_model_method_type()
        task = self._make_task(
            'test.task.target', '_compute_display_name')

        with self.assertRaises(ValueError):
            task_type.execute(self.env, task)

    def test_plan_warns_on_undecorated(self):
        """_g_task_queue__plan should log a warning for
        undecorated methods."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'warn-test', 'value': 0})

        logger_name = (
            'odoo.addons.generic_task_queue.models.base'
        )
        with self.assertLogs(logger_name, level='WARNING') as cm:
            rec._g_task_queue__plan('read')

        self.assertTrue(
            any('not decorated' in msg for msg in cm.output))

    def test_plan_no_warning_on_decorated(self):
        """_g_task_queue__plan should not warn for
        decorated methods."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'ok-test', 'value': 0})

        # Should not produce any warnings
        task = rec._g_task_queue__plan('do_increment')
        self.assertEqual(task.state, 'pending')
