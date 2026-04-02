from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_type import (
    AbstractTaskType,
)
from odoo.addons.generic_task_queue.service.task_type_registry import (
    TaskTypeRegistry,
)


class TestTaskTypeRegistration(TransactionCase):
    """Test that task types are auto-registered via __init_subclass__."""

    def test_test_task_types_registered(self):
        """Task types defined in test_generic_task_queue/service/
        should be in the registry."""
        registry = TaskTypeRegistry()
        types = registry.get_initialized_types()

        self.assertIn('test.task.type.noop', types)
        self.assertIn('test.task.type.echo', types)

    def test_model_method_type_registered(self):
        """The built-in ModelMethodTaskType should be registered."""
        registry = TaskTypeRegistry()
        types = registry.get_initialized_types()

        self.assertIn('task.type.model.method', types)

    def test_get_task_type_returns_class(self):
        """get_task_type() should return the merged class
        for a valid type code."""
        registry = TaskTypeRegistry()
        task_type_cls = registry.get_task_type('test.task.type.noop')

        self.assertTrue(
            issubclass(task_type_cls, AbstractTaskType))

    def test_get_task_type_unknown_raises(self):
        """get_task_type() should raise KeyError for
        unknown type codes."""
        registry = TaskTypeRegistry()

        with self.assertRaises(KeyError):
            registry.get_task_type('nonexistent.task.type')

    def test_registry_is_singleton(self):
        """Multiple TaskTypeRegistry() calls should return
        the same instance."""
        r1 = TaskTypeRegistry()
        r2 = TaskTypeRegistry()
        self.assertIs(r1, r2)

    def test_late_registration_accepted(self):
        """Task types registered after initialization should
        be available via lazy build."""
        registry = TaskTypeRegistry()

        TaskTypeRegistry.register_type(
            'late.task.type.test', type('LateType', (AbstractTaskType,), {
                '_name': None,  # prevent __init_subclass__ re-register
                'execute': lambda self, env, task: None,
            }))

        # Should be accessible via get_task_type (lazy build)
        cls = registry.get_task_type('late.task.type.test')
        self.assertTrue(cls)

        # Cleanup
        TaskTypeRegistry._registered_types.pop(
            'late.task.type.test', None)
        TaskTypeRegistry._initialized_types.pop(
            'late.task.type.test', None)

    def test_none_name_not_registered(self):
        """Classes with _name = None should not be registered."""
        registry = TaskTypeRegistry()
        types = registry.get_initialized_types()

        # AbstractTaskType has _name = None — should not be in registry
        self.assertNotIn(None, types)


class TestTaskTypeInstantiation(TransactionCase):
    """Test that registered task types can be instantiated."""

    def test_instantiate_noop(self):
        """Registered task type classes should be instantiable."""
        registry = TaskTypeRegistry()
        cls = registry.get_task_type('test.task.type.noop')
        instance = cls()
        self.assertIsInstance(instance, AbstractTaskType)
