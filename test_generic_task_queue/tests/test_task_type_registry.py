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


class TestTaskTypeExtension(TransactionCase):
    """Test MRO-based merging of multiple definitions sharing one _name.

    Two classes in test_generic_task_queue/service/test_task_types.py declare
    _name = 'test.task.type.extended': a base and an extension defined after
    it. The registry merges them into type(name, (Extension, Base), {}); in
    Python's MRO the first base wins, so the later-registered *extension* must
    take precedence — the behaviour the register_type() fix guarantees.
    """

    def _extended(self):
        return TaskTypeRegistry().get_task_type('test.task.type.extended')

    def test_extension_wins_over_base(self):
        """A class attribute overridden by the extension wins."""
        self.assertEqual(self._extended()()._marker, 'override')

    def test_extension_mro_order(self):
        """The extension appears before the base in the merged MRO."""
        names = [c.__name__ for c in self._extended().__mro__]
        self.assertIn('TestTaskTypeExtOverride', names)
        self.assertIn('TestTaskTypeExtBase', names)
        self.assertLess(
            names.index('TestTaskTypeExtOverride'),
            names.index('TestTaskTypeExtBase'),
            'extension must precede the base in the MRO')

    def test_extension_super_chains_to_base(self):
        """super() in the extension cooperatively reaches the base def.

        The extension's execute() calls super().execute(); the merged result
        therefore carries both definitions' contributions ('origin' set by the
        extension, plus the base having run first).
        """
        inst = self._extended()()
        empty_task = self.env['generic.task.queue.task']
        self.assertEqual(
            inst.execute(self.env, empty_task), {'origin': 'override'})

    def test_base_only_member_still_inherited(self):
        """Members only the base defines remain reachable on the merged class.

        Guards against a merge that would drop the base entirely rather than
        layering the extension on top of it.
        """
        self.assertEqual(self._extended()().base_only(), 'base-only')


class TestRegisterTypeOrdering(TransactionCase):
    """Directly exercise register_type()'s newest-first ordering contract.

    These are self-contained (no reliance on import order) and clean up the
    scratch registrations they create.
    """

    def _make_type(self, marker):
        return type('ScratchType', (AbstractTaskType,), {
            '_name': None,  # prevent __init_subclass__ auto-registration
            '_marker': marker,
            'execute': lambda self, env, task: marker,
        })

    def _cleanup(self, name):
        TaskTypeRegistry._registered_types.pop(name, None)
        TaskTypeRegistry._initialized_types.pop(name, None)

    def test_newest_registration_is_first(self):
        """Each new definition is inserted at the front of the list."""
        name = 'scratch.mro.order'
        base = self._make_type('base')
        ext = self._make_type('ext')
        try:
            TaskTypeRegistry.register_type(name, base)
            TaskTypeRegistry.register_type(name, ext)
            self.assertEqual(
                TaskTypeRegistry._registered_types[name], [ext, base])
            merged = TaskTypeRegistry().get_task_type(name)
            self.assertEqual(merged()._marker, 'ext')
        finally:
            self._cleanup(name)

    def test_last_of_three_wins(self):
        """With three stacked definitions the last registered wins, and all
        remain in the merged MRO."""
        name = 'scratch.mro.three'
        a, b, c = (self._make_type(m) for m in ('a', 'b', 'c'))
        try:
            for t in (a, b, c):
                TaskTypeRegistry.register_type(name, t)
            merged = TaskTypeRegistry().get_task_type(name)
            self.assertEqual(merged()._marker, 'c')
            for t in (a, b, c):
                self.assertIn(t, merged.__mro__)
        finally:
            self._cleanup(name)

    def test_reregistration_invalidates_cache(self):
        """Registering a new extension after the type was already built
        rebuilds the merged class so the extension takes effect."""
        name = 'scratch.mro.reinit'
        base = self._make_type('base')
        ext = self._make_type('ext')
        try:
            TaskTypeRegistry.register_type(name, base)
            first = TaskTypeRegistry().get_task_type(name)
            self.assertEqual(first()._marker, 'base')

            TaskTypeRegistry.register_type(name, ext)
            second = TaskTypeRegistry().get_task_type(name)
            self.assertEqual(second()._marker, 'ext')
            self.assertIsNot(first, second)
        finally:
            self._cleanup(name)


class TestTaskTypeInstantiation(TransactionCase):
    """Test that registered task types can be instantiated."""

    def test_instantiate_noop(self):
        """Registered task type classes should be instantiable."""
        registry = TaskTypeRegistry()
        cls = registry.get_task_type('test.task.type.noop')
        instance = cls()
        self.assertIsInstance(instance, AbstractTaskType)
