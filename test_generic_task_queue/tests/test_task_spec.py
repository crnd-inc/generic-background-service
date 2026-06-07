from odoo import exceptions
from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue import TaskSpec, TaskListSpec


class TestTaskSpec(TransactionCase):
    """Unit tests for the TaskSpec value object (pure Python)."""

    def test_to_vals_minimal(self):
        spec = TaskSpec('my.type', {'a': 1})
        self.assertEqual(
            spec.to_vals(),
            {'type_code': 'my.type', 'task_params': {'a': 1}})

    def test_to_vals_defaults_params_to_empty(self):
        self.assertEqual(
            TaskSpec('my.type').to_vals(),
            {'type_code': 'my.type', 'task_params': {}})

    def test_to_vals_includes_only_set_overrides(self):
        spec = TaskSpec('my.type', {'a': 1}, channel='fast', priority=0)
        self.assertEqual(spec.to_vals(), {
            'type_code': 'my.type', 'task_params': {'a': 1},
            'channel': 'fast', 'priority': 0,
        })
        # priority=0 is set (not None) → included
        self.assertIn('priority', spec.to_vals())
        # unset fields are omitted
        self.assertNotIn('timeout', spec.to_vals())

    def test_replace_returns_new_instance(self):
        spec = TaskSpec('my.type', {'a': 1}, priority=5)
        other = spec.replace(priority=1)
        self.assertEqual(spec.priority, 5)      # original untouched (frozen)
        self.assertEqual(other.priority, 1)
        self.assertEqual(other.type_code, 'my.type')

    def test_is_immutable(self):
        spec = TaskSpec('my.type')
        with self.assertRaises(Exception):
            spec.priority = 1                   # frozen dataclass

    def test_validation_rejects_empty_type_code(self):
        with self.assertRaises(ValueError):
            TaskSpec('')
        with self.assertRaises(ValueError):
            TaskSpec(None)

    def test_validation_rejects_non_dict_params(self):
        with self.assertRaises(ValueError):
            TaskSpec('my.type', [1, 2])

    def test_unknown_field_rejected(self):
        with self.assertRaises(TypeError):
            TaskSpec('my.type', {}, state='done')   # not a whitelisted field


class TestTaskListSpec(TransactionCase):
    """Unit tests for the TaskListSpec builder."""

    def test_add_and_iterate(self):
        batch = (TaskListSpec()
                 .add('a', {'x': 1})
                 .add('b', {'y': 2}))
        self.assertEqual(len(batch), 2)
        self.assertTrue(batch)
        specs = list(batch)
        self.assertEqual([s.type_code for s in specs], ['a', 'b'])
        self.assertEqual(specs[0].params, {'x': 1})

    def test_empty_is_falsy(self):
        self.assertFalse(TaskListSpec())
        self.assertEqual(len(TaskListSpec()), 0)

    def test_defaults_applied_and_overridden(self):
        batch = (TaskListSpec(channel='heavy', priority=5)
                 .add('a', {})                        # inherits both defaults
                 .add('b', {}, channel='fast'))       # overrides channel
        specs = list(batch)
        self.assertEqual((specs[0].channel, specs[0].priority),
                         ('heavy', 5))
        self.assertEqual((specs[1].channel, specs[1].priority),
                         ('fast', 5))

    def test_add_many(self):
        batch = TaskListSpec(channel='heavy').add_many(
            'chunk', [{'i': 0}, {'i': 1}, {'i': 2}])
        self.assertEqual(len(batch), 3)
        self.assertEqual(set(s.channel for s in batch), {'heavy'})

    def test_add_spec_and_extend(self):
        s1, s2 = TaskSpec('a', {}), TaskSpec('b', {})
        batch = TaskListSpec().add_spec(s1).extend([s2])
        self.assertEqual(list(batch), [s1, s2])

    def test_add_spec_type_checked(self):
        with self.assertRaises(TypeError):
            TaskListSpec().add_spec(('a', {}))   # not a TaskSpec


class TestCreateChildrenSpecs(TransactionCase):
    """create_children heterogeneous form (iterable of TaskSpec)."""

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.parent = self.Task.create_task('test.task.type.noop', name='P')

    def test_list_of_taskspec(self):
        children = self.Task.create_children(self.parent, [
            TaskSpec('test.task.type.echo', {'a': 1}),
            TaskSpec('test.task.type.noop', {'b': 2},
                     channel='fast', priority=1),
        ])
        self.assertEqual(len(children), 2)
        self.assertEqual(
            children.mapped('type_code'),
            ['test.task.type.echo', 'test.task.type.noop'])
        notify = children.filtered(
            lambda c: c.type_code == 'test.task.type.noop')
        self.assertEqual((notify.channel, notify.priority), ('fast', 1))
        echo = children.filtered(
            lambda c: c.type_code == 'test.task.type.echo')
        self.assertEqual(echo.channel, self.parent.channel)   # inherited

    def test_tasklistspec(self):
        batch = TaskListSpec(channel='heavy').add_many(
            'test.task.type.echo', [{'i': 0}, {'i': 1}])
        children = self.Task.create_children(self.parent, batch)
        self.assertEqual(len(children), 2)
        self.assertEqual(set(children.mapped('channel')), {'heavy'})

    def test_name_override_from_spec(self):
        children = self.Task.create_children(self.parent, [
            TaskSpec('test.task.type.noop', {}, name='custom name'),
        ])
        self.assertEqual(children.name, 'custom name')

    def test_rejects_non_taskspec(self):
        with self.assertRaises(exceptions.ValidationError):
            self.Task.create_children(
                self.parent, [('test.task.type.noop', {})])

    def test_rejects_mixing_forms(self):
        with self.assertRaises(exceptions.ValidationError):
            self.Task.create_children(
                self.parent,
                [TaskSpec('test.task.type.noop', {})],
                channel='x')

    def test_homogeneous_form_unchanged(self):
        children = self.Task.create_children(
            self.parent, 'test.task.type.echo',
            [{'a': 1}, {'b': 2}], priority=3)
        self.assertEqual(len(children), 2)
        self.assertEqual(set(children.mapped('priority')), {3})


class TestCreateTasks(TransactionCase):
    """Top-level create_tasks batch from TaskSpec."""

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']

    def test_creates_top_level_tasks(self):
        tasks = self.Task.create_tasks([
            TaskSpec('test.task.type.echo', {'a': 1}),
            TaskSpec('test.task.type.noop', {'b': 2}, priority=1),
        ])
        self.assertEqual(len(tasks), 2)
        self.assertFalse(any(tasks.mapped('parent_id')))    # top-level
        self.assertEqual(set(tasks.mapped('state')), {'pending'})

    def test_channel_falls_back_to_type_default(self):
        # test.routing.custom.channel has _default_channel='heavy'
        tasks = self.Task.create_tasks([
            TaskSpec('test.routing.custom.channel', {})])
        self.assertEqual(tasks.channel, 'heavy')

    def test_explicit_channel_wins(self):
        tasks = self.Task.create_tasks([
            TaskSpec('test.routing.custom.channel', {}, channel='override')])
        self.assertEqual(tasks.channel, 'override')

    def test_name_defaults_to_type_code(self):
        tasks = self.Task.create_tasks([TaskSpec('test.task.type.noop', {})])
        self.assertEqual(tasks.name, 'test.task.type.noop')

    def test_with_tasklistspec(self):
        batch = TaskListSpec().add('test.task.type.noop', {})
        tasks = self.Task.create_tasks(batch)
        self.assertEqual(len(tasks), 1)

    def test_rejects_non_taskspec(self):
        with self.assertRaises(exceptions.ValidationError):
            self.Task.create_tasks([{'type_code': 'test.task.type.noop'}])
