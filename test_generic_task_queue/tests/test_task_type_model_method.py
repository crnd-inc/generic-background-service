from unittest.mock import MagicMock

from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_type_registry import (
    TaskTypeRegistry,
)


class TestModelMethodTaskType(TransactionCase):
    """Test the built-in ModelMethodTaskType."""

    def _make_task_mock(self, params):
        """Create a mock task object with the given params."""
        task = MagicMock()
        task.task_params = params
        return task

    def _get_task_type(self):
        """Get an instance of ModelMethodTaskType."""
        registry = TaskTypeRegistry()
        cls = registry.get_task_type('task.type.model.method')
        return cls()

    def test_calls_model_method(self):
        """ModelMethodTaskType should call the specified model method
        on the given record IDs."""
        Target = self.env['test.task.target']
        rec = Target.create({'name': 'test', 'value': 10})

        task_type = self._get_task_type()
        task = self._make_task_mock({
            'model': 'test.task.target',
            'method': 'do_increment',
            'record_ids': [rec.id],
            'kwargs': {'amount': 5},
        })

        task_type.execute(self.env, task)

        rec.invalidate_recordset()
        self.assertEqual(rec.value, 15)
        self.assertTrue(rec.processed)

    def test_multiple_records(self):
        """Should work on multiple record IDs."""
        Target = self.env['test.task.target']
        r1 = Target.create({'name': 'a', 'value': 0})
        r2 = Target.create({'name': 'b', 'value': 0})

        task_type = self._get_task_type()
        task = self._make_task_mock({
            'model': 'test.task.target',
            'method': 'do_increment',
            'record_ids': [r1.id, r2.id],
        })

        task_type.execute(self.env, task)

        r1.invalidate_recordset()
        r2.invalidate_recordset()
        self.assertEqual(r1.value, 1)
        self.assertEqual(r2.value, 1)

    def test_empty_record_ids(self):
        """Should handle empty record_ids (calls method on empty recordset)."""
        task_type = self._get_task_type()
        task = self._make_task_mock({
            'model': 'test.task.target',
            'method': 'do_increment',
        })

        # Should not raise
        task_type.execute(self.env, task)

    def test_rejects_private_methods(self):
        """Should reject methods starting with underscore."""
        task_type = self._get_task_type()
        task = self._make_task_mock({
            'model': 'test.task.target',
            'method': '_compute_display_name',
            'record_ids': [],
        })

        with self.assertRaises(ValueError) as cm:
            task_type.execute(self.env, task)
        self.assertIn('private', str(cm.exception))

    def test_rejects_forbidden_methods(self):
        """Should reject dangerous methods like unlink, write, create."""
        task_type = self._get_task_type()

        for method in ('unlink', 'write', 'create'):
            task = self._make_task_mock({
                'model': 'test.task.target',
                'method': method,
                'record_ids': [],
            })
            with self.assertRaises(ValueError, msg=method):
                task_type.execute(self.env, task)

    def test_rejects_unknown_model(self):
        """Should reject a model name that doesn't exist."""
        task_type = self._get_task_type()
        task = self._make_task_mock({
            'model': 'nonexistent.model.xyz',
            'method': 'do_something',
            'record_ids': [],
        })

        with self.assertRaises(ValueError) as cm:
            task_type.execute(self.env, task)
        self.assertIn('not available', str(cm.exception))
