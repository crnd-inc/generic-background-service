import os
import shutil
import tempfile
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_type_registry import (
    TaskTypeRegistry,
)


def _get_task_type():
    return TaskTypeRegistry().get_task_type(
        'task.type.background.migration')()


class TestTaskTypeAttributes(TransactionCase):
    """Verify the task type is registered with the expected attributes."""

    def test_registered_in_registry(self):
        cls = TaskTypeRegistry().get_task_type(
            'task.type.background.migration')
        self.assertIsNotNone(cls)

    def test_not_singleton(self):
        cls = TaskTypeRegistry().get_task_type(
            'task.type.background.migration')
        self.assertFalse(cls._singleton)

    def test_default_channel_is_background_migration(self):
        cls = TaskTypeRegistry().get_task_type(
            'task.type.background.migration')
        self.assertEqual(cls._default_channel, 'background_migration')

    def test_task_created_on_background_migration_channel(self):
        """create_task() picks up _default_channel automatically."""
        task = self.env['generic.task.queue.task'].create_task(
            'task.type.background.migration',
            params={'module': 'base', 'version': '18.0.1.0.0',
                    'name': 'test'},
        )
        self.assertEqual(task.channel, 'background_migration')


class TestParamValidation(TransactionCase):
    """execute() must reject params that fail the security regex."""

    def _make_task(self, module, version, name):
        return self.env['generic.task.queue.task'].create({
            'name': 'validation test',
            'type_code': 'task.type.background.migration',
            'task_params': {
                'module': module,
                'version': version,
                'name': name,
            },
        })

    def test_invalid_module_name(self):
        task = self._make_task('../evil', '18.0.1.0.0', 'test')
        with self.assertRaises(ValueError):
            _get_task_type().execute(self.env, task)

    def test_invalid_version(self):
        task = self._make_task('my_module', '../../etc/passwd', 'test')
        with self.assertRaises(ValueError):
            _get_task_type().execute(self.env, task)

    def test_invalid_migration_name(self):
        task = self._make_task('my_module', '18.0.1.0.0', '../evil')
        with self.assertRaises(ValueError):
            _get_task_type().execute(self.env, task)

    def test_module_name_with_dot_rejected(self):
        task = self._make_task('my.module', '18.0.1.0.0', 'test')
        with self.assertRaises(ValueError):
            _get_task_type().execute(self.env, task)

    def test_version_too_few_parts_rejected(self):
        task = self._make_task('my_module', '1.0.0', 'test')
        with self.assertRaises(ValueError):
            _get_task_type().execute(self.env, task)

    def test_valid_params_with_unknown_module_return_none(self):
        """Valid params, but module not installed — silently returns None."""
        task = self._make_task('nonexistent_xyz_module', '18.0.1.0.0', 'test')
        result = _get_task_type().execute(self.env, task)
        self.assertIsNone(result)


class TestMigrationFileExecution(TransactionCase):
    """execute() loads and calls migrate() from the file."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.module = 'fake_test_module'
        self.version = '18.0.1.0.0'
        self.name = 'my-migration'
        migration_dir = os.path.join(
            self.tmpdir, 'migrations', self.version)
        os.makedirs(migration_dir)
        self.migration_path = os.path.join(
            migration_dir, 'background-%s.py' % self.name)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_task(self, name=None):
        return self.env['generic.task.queue.task'].create({
            'name': 'exec test',
            'type_code': 'task.type.background.migration',
            'task_params': {
                'module': self.module,
                'version': self.version,
                'name': name or self.name,
            },
        })

    def _run(self, task=None):
        task = task or self._make_task()
        _patch = patch(
            'odoo.addons.generic_background_migration'
            '.task_type.background_migration.get_module_path',
            return_value=self.tmpdir)
        with _patch:
            return _get_task_type().execute(self.env, task)

    def _write(self, content):
        with open(self.migration_path, 'w') as f:
            f.write(content)

    def test_env_only_signature(self):
        self._write('def migrate(env):\n    return "env-only"\n')
        self.assertEqual(self._run(), 'env-only')

    def test_env_task_signature(self):
        self._write('def migrate(env, task):\n    return "env-task"\n')
        self.assertEqual(self._run(), 'env-task')

    def test_env_is_passed_correctly(self):
        self._write(
            'def migrate(env):\n'
            '    return env["res.users"].browse(1).exists() and "ok"\n')
        self.assertEqual(self._run(), 'ok')

    def test_missing_file_returns_none(self):
        task = self._make_task(name='nonexistent-migration')
        result = self._run(task=task)
        self.assertIsNone(result)

    def test_missing_migrate_function_raises(self):
        self._write('# no migrate function here\n')
        with self.assertRaises(AttributeError):
            self._run()

    def test_return_value_propagated(self):
        self._write('def migrate(env, task):\n    return {"done": 42}\n')
        self.assertEqual(self._run(), {'done': 42})


class TestMigrationScheduling(TransactionCase):
    """_schedule_migration() record lifecycle and deduplication."""

    def setUp(self):
        super().setUp()
        self.Migration = self.env['generic.background.migration']

    def _search(self, module, version, name):
        return self.Migration.sudo().search([
            ('module', '=', module),
            ('module_version', '=', version),
            ('migration_name', '=', name),
        ], limit=1)

    def test_new_migration_creates_record_and_task(self):
        self.Migration._schedule_migration(
            'sched_mod', '18.0.1.0.0', 'new-mig')
        rec = self._search('sched_mod', '18.0.1.0.0', 'new-mig')

        self.assertTrue(rec)
        self.assertEqual(rec.state, 'pending')
        self.assertEqual(
            rec.name, 'sched_mod/18.0.1.0.0/new-mig')
        self.assertTrue(rec.task_id)
        self.assertEqual(rec.task_id.state, 'pending')
        self.assertEqual(
            rec.task_id.type_code, 'task.type.background.migration')
        params = rec.task_id.task_params
        self.assertEqual(params['module'], 'sched_mod')
        self.assertEqual(params['version'], '18.0.1.0.0')
        self.assertEqual(params['name'], 'new-mig')

    def test_done_migration_is_skipped(self):
        self.Migration._schedule_migration(
            'sched_mod', '18.0.1.0.0', 'done-mig')
        rec = self._search('sched_mod', '18.0.1.0.0', 'done-mig')
        rec.sudo().write({'state': 'done'})
        task_id_before = rec.task_id.id

        self.Migration._schedule_migration(
            'sched_mod', '18.0.1.0.0', 'done-mig')

        rec.invalidate_recordset()
        self.assertEqual(rec.state, 'done')
        self.assertEqual(rec.task_id.id, task_id_before)

    def test_failed_migration_is_rescheduled(self):
        self.Migration._schedule_migration(
            'sched_mod', '18.0.1.0.0', 'fail-mig')
        rec = self._search('sched_mod', '18.0.1.0.0', 'fail-mig')
        rec.sudo().write({'state': 'failed'})

        self.Migration._schedule_migration(
            'sched_mod', '18.0.1.0.0', 'fail-mig')

        rec.invalidate_recordset()
        self.assertEqual(rec.state, 'pending')
        self.assertTrue(rec.task_id)

    def test_pending_migration_deduplicates_task(self):
        """Scheduling a still-pending migration reuses the existing task."""
        self.Migration._schedule_migration(
            'sched_mod', '18.0.1.0.0', 'dedup-mig')
        rec = self._search('sched_mod', '18.0.1.0.0', 'dedup-mig')
        task_id_first = rec.task_id.id

        self.Migration._schedule_migration(
            'sched_mod', '18.0.1.0.0', 'dedup-mig')

        rec.invalidate_recordset()
        self.assertEqual(rec.task_id.id, task_id_first)

    def test_different_versions_are_independent(self):
        """Same module+name in different version directories → two records."""
        self.Migration._schedule_migration(
            'sched_mod', '18.0.1.0.0', 'shared-name')
        self.Migration._schedule_migration(
            'sched_mod', '18.0.2.0.0', 'shared-name')

        recs = self.Migration.sudo().search([
            ('module', '=', 'sched_mod'),
            ('migration_name', '=', 'shared-name'),
        ])
        self.assertEqual(len(recs), 2)
        versions = set(recs.mapped('module_version'))
        self.assertEqual(versions, {'18.0.1.0.0', '18.0.2.0.0'})


class TestMigrationDiscovery(TransactionCase):
    """_schedule_module_migrations() filesystem scan."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.Migration = self.env['generic.background.migration']

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_file(self, version, name):
        d = os.path.join(self.tmpdir, 'migrations', version)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'background-%s.py' % name), 'w') as f:
            f.write('def migrate(env): pass\n')

    def _scan(self, module='fake_module'):
        _patch = patch(
            'odoo.addons.generic_background_migration'
            '.models.generic_background_migration.get_module_path',
            return_value=self.tmpdir)
        with _patch:
            self.Migration._schedule_module_migrations(module)

    def _records(self, module='fake_module'):
        return self.Migration.sudo().search([('module', '=', module)])

    def test_discovers_files_across_versions(self):
        self._write_file('18.0.1.0.0', 'first')
        self._write_file('18.0.1.0.0', 'second')
        self._write_file('18.0.2.0.0', 'third')

        self._scan()

        recs = self._records()
        self.assertEqual(len(recs), 3)
        self.assertEqual(
            set(recs.mapped('migration_name')),
            {'first', 'second', 'third'})

    def test_ignores_non_version_directories(self):
        self._write_file('18.0.1.0.0', 'valid')
        # Write a file under a non-version directory (e.g. __pycache__)
        bad_dir = os.path.join(self.tmpdir, 'migrations', '__pycache__')
        os.makedirs(bad_dir, exist_ok=True)
        with open(os.path.join(bad_dir, 'background-evil.py'), 'w') as f:
            f.write('def migrate(env): pass\n')

        self._scan()

        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].migration_name, 'valid')

    def test_ignores_invalid_migration_names(self):
        # Filename with dot in the name part (not valid per _NAME_RE)
        d = os.path.join(self.tmpdir, 'migrations', '18.0.1.0.0')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'background-evil.name.py'), 'w') as f:
            f.write('def migrate(env): pass\n')

        self._scan()

        self.assertEqual(len(self._records()), 0)

    def test_no_migrations_dir_is_silent(self):
        """Module with no migrations/ directory → no records, no exception."""
        self._scan()
        self.assertEqual(len(self._records()), 0)

    def test_unknown_module_is_silent(self):
        """Module path not found → no records, no exception."""
        _patch = patch(
            'odoo.addons.generic_background_migration'
            '.models.generic_background_migration.get_module_path',
            return_value=None)
        with _patch:
            self.Migration._schedule_module_migrations('nonexistent')
        self.assertEqual(len(self._records('nonexistent')), 0)

    def test_already_done_migration_not_rescheduled(self):
        self._write_file('18.0.1.0.0', 'idempotent')
        self._scan()

        rec = self.Migration.sudo().search([
            ('module', '=', 'fake_module'),
            ('migration_name', '=', 'idempotent'),
        ])
        rec.sudo().write({'state': 'done'})
        task_id_before = rec.task_id.id

        self._scan()

        rec.invalidate_recordset()
        self.assertEqual(rec.state, 'done')
        self.assertEqual(rec.task_id.id, task_id_before)


class TestMigrationHooks(TransactionCase):
    """on_success, on_failure, on_all_children_done update the DB record."""

    def setUp(self):
        super().setUp()
        Migration = self.env['generic.background.migration']
        Migration._schedule_migration('hook_mod', '18.0.1.0.0', 'hook-test')
        self.rec = Migration.sudo().search([
            ('module', '=', 'hook_mod'),
            ('module_version', '=', '18.0.1.0.0'),
            ('migration_name', '=', 'hook-test'),
        ])
        self.task = self.rec.task_id

    def test_on_success_marks_done(self):
        _get_task_type().on_success(self.env, self.task, result=None)
        self.rec.invalidate_recordset()
        self.assertEqual(self.rec.state, 'done')
        self.assertTrue(self.rec.date_completed)

    def test_on_failure_marks_failed(self):
        _get_task_type().on_failure(
            self.env, self.task, exc=RuntimeError('boom'))
        self.rec.invalidate_recordset()
        self.assertEqual(self.rec.state, 'failed')
        self.assertTrue(self.rec.date_completed)

    def test_on_all_children_done_marks_done_and_aggregates(self):
        worker = self.env['generic.task.queue.worker'].create({
            'uuid': 'hook-test-worker',
            'service_name': 'test.service',
            'state': 'active',
        })
        Task = self.env['generic.task.queue.task']

        # Create two child tasks
        child1 = Task.create_task(
            'task.type.background.migration',
            params={'module': 'hook_mod', 'version': '18.0.1.0.0',
                    'name': 'child-1'},
            parent_id=self.task.id,
        )
        child2 = Task.create_task(
            'task.type.background.migration',
            params={'module': 'hook_mod', 'version': '18.0.1.0.0',
                    'name': 'child-2'},
            parent_id=self.task.id,
        )
        # Drive child1 to done, child2 to cancelled — mirrors what
        # _check_waiting_parent guarantees when the hook fires in production
        # (no failed children remain; they'd cause action_fail instead).
        child1.action_assign(worker)
        child1.sudo().action_start()
        child1.sudo().action_done({'x': 1})

        child2.action_cancel()

        result = _get_task_type().on_all_children_done(self.env, self.task)

        self.rec.invalidate_recordset()
        self.assertEqual(self.rec.state, 'done')
        self.assertTrue(self.rec.date_completed)
        self.assertEqual(result['children_done'], 1)
        self.assertEqual(result['children_cancelled'], 1)


# ---------------------------------------------------------------------------
# BackgroundServiceRegistry composition
# ---------------------------------------------------------------------------

class TestComposedServiceChannels(TransactionCase):
    """BackgroundServiceRegistry must compose generic.task.queue.service
    so that the generic_background_migration extension has higher MRO
    priority than the base TaskQueueService, and _get_channels() returns
    both 'default' and 'background_migration'.
    """

    _SERVICE_NAME = 'generic.task.queue.service'

    @classmethod
    def _get_composed_cls(cls):
        from odoo.addons.generic_background_service.service.background_service_registry import (  # noqa: E501
            BackgroundServiceRegistry,
        )
        # Trigger initialize() if this is the first call in this process.
        # The singleton guard makes repeated calls safe.
        BackgroundServiceRegistry()
        return BackgroundServiceRegistry.get_service_class(cls._SERVICE_NAME)

    def test_service_present_in_registry(self):
        """Composed service is present in the initialized registry."""
        self.assertIsNotNone(self._get_composed_cls())

    def test_get_channels_contains_default_and_background_migration(self):
        """_get_channels() on the composed service includes both channels."""
        cls = self._get_composed_cls()
        channels = cls.__new__(cls)._get_channels()
        self.assertIn('default', channels)
        self.assertIn('background_migration', channels)

    def test_get_channels_order(self):
        """default precedes background_migration in the channel list."""
        cls = self._get_composed_cls()
        channels = cls.__new__(cls)._get_channels()
        self.assertEqual(channels, ['default', 'background_migration'])

    def test_migration_extension_precedes_base_in_mro(self):
        """Last-registered class (extension) has higher MRO priority than
        the base, so its _get_channels() override is reached first and
        super() correctly delegates to the base."""
        from odoo.addons.generic_task_queue.service.task_queue_service import (
            TaskQueueService as Base,
        )
        from odoo.addons.generic_background_migration.service.task_queue_service import (  # noqa: E501
            TaskQueueService as Extension,
        )
        composed = self._get_composed_cls()
        mro = composed.__mro__
        self.assertIn(Base, mro)
        self.assertIn(Extension, mro)
        self.assertLess(
            mro.index(Extension), mro.index(Base),
            "Extension must appear before Base in MRO "
            "(last-registered = highest priority)",
        )

    def test_get_worker_params_channels(self):
        """get_worker_params()['channels'] reflects the composed channels."""
        cls = self._get_composed_cls()
        params = cls.__new__(cls).get_worker_params()
        self.assertIn('default', params['channels'])
        self.assertIn('background_migration', params['channels'])
