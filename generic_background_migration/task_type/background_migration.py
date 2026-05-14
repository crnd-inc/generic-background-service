import importlib.util
import inspect
import logging
import os
import re

from odoo import fields
from odoo.modules import get_module_path

from odoo.addons.generic_task_queue import AbstractTaskType

_logger = logging.getLogger(__name__)

_MODULE_RE = re.compile(r'^[a-zA-Z0-9_]+$')
_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+\.\d+\.\d+$')
_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


class BackgroundMigrationTaskType(AbstractTaskType):
    """Runs file-based background migrations.

    Task params:
        module  — Odoo module technical name (e.g. 'my_module')
        version — migration directory version string (e.g. '18.0.1.2.3')
        name    — bare migration name (e.g. 'recompute-fields' from
                  migrations/18.0.1.2.3/background-recompute-fields.py)

    The migration file must export ``migrate(env, task)`` or ``migrate(env)``.
    """

    _name = 'task.type.background.migration'
    _singleton = False
    _retry_policy = 'retry_known'
    _track_progress = True
    _default_channel = 'background_migration'

    def execute(self, env, task):
        params = task.task_params
        module = params['module']
        version = params['version']
        name = params['name']

        if not _MODULE_RE.match(module):
            raise ValueError("Invalid module name: %r" % module)
        if not _VERSION_RE.match(version):
            raise ValueError("Invalid version: %r" % version)
        if not _NAME_RE.match(name):
            raise ValueError("Invalid migration name: %r" % name)

        module_path = get_module_path(module, display_warning=False)
        if not module_path:
            _logger.warning(
                "Background migration %s/%s/%s: module path not found "
                "(module may have been uninstalled).",
                module, version, name)
            return

        migration_file = os.path.join(
            module_path, 'migrations', version,
            'background-%s.py' % name)

        if not os.path.isfile(migration_file):
            _logger.warning(
                "Background migration file %r not found "
                "(module may have been uninstalled).",
                migration_file)
            return

        migrate_fn = self._load_migrate_fn(
            migration_file, module, version, name)
        return migrate_fn(env, task)

    def _load_migrate_fn(self, filepath, module, version, name):
        spec = importlib.util.spec_from_file_location(
            'generic_background_migration._file.%s.%s.%s' % (
                module,
                version.replace('.', '_'),
                name.replace('-', '_')),
            filepath)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        fn = getattr(mod, 'migrate', None)
        if fn is None:
            raise AttributeError(
                "Migration file %r has no 'migrate' function." % filepath)

        # Support both migrate(env) and migrate(env, task)
        if len(inspect.signature(fn).parameters) == 1:
            return lambda env, task: fn(env)
        return fn

    def on_success(self, env, task, result):
        self._mark_migration(env, task, 'done')

    def on_all_children_done(self, env, parent_task):
        self._mark_migration(env, parent_task, 'done')
        results = list(self.iter_child_results(parent_task))
        done = sum(1 for cr in results if cr.state == 'done')
        cancelled = sum(1 for cr in results if cr.state == 'cancelled')
        return {'children_done': done, 'children_cancelled': cancelled}

    def on_failure(self, env, task, exc):
        self._mark_migration(env, task, 'failed')

    def _get_migration_record(self, env, task):
        params = task.task_params
        return env['generic.background.migration'].sudo().search([
            ('module', '=', params['module']),
            ('module_version', '=', params['version']),
            ('migration_name', '=', params['name']),
        ], limit=1)

    def _mark_migration(self, env, task, state):
        rec = self._get_migration_record(env, task)
        if not rec:
            _logger.warning(
                "Migration record not found for task %s — cannot mark %s.",
                task.id, state)
            return
        rec.write({'state': state, 'date_completed': fields.Datetime.now()})
