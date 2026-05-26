import glob
import logging
import os
import re

from odoo import models, fields, api
from odoo.modules import get_module_path

_logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+\.\d+\.\d+$')
_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


class GenericBackgroundMigration(models.Model):
    _name = 'generic.background.migration'
    _description = 'Background Migration'
    _order = 'module, module_version, migration_name'

    name = fields.Char(
        required=True, readonly=True, index=True,
        help="Composite identifier: module/version/migration_name.")
    module = fields.Char(
        required=True, readonly=True, index=True,
        help="Technical name of the Odoo module that owns this migration.")
    module_version = fields.Char(
        required=True, readonly=True,
        help="Migration directory version (e.g. '18.0.1.2.3').")
    migration_name = fields.Char(
        required=True, readonly=True,
        help="Bare migration name (e.g. 'recompute-fields' from "
             "background-recompute-fields.py).")
    state = fields.Selection([
        ('pending', 'Pending'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], default='pending', required=True, index=True, readonly=True,
        help="pending — waiting to run or actively executing.\n"
             "done    — completed successfully.\n"
             "failed  — last attempt failed permanently; will be "
             "re-scheduled on the next module upgrade.")
    task_id = fields.Many2one(
        'generic.task.queue.task',
        ondelete='set null', readonly=True,
        help="Current or most recent task executing this migration.")
    date_completed = fields.Datetime(
        readonly=True,
        help="When the migration last reached a terminal state "
             "(done or failed).")

    _sql_constraints = [
        ('module_version_name_uniq',
         'UNIQUE (module, module_version, migration_name)',
         'Migration must be unique per module/version/name.'),
    ]

    def _register_hook(self):
        """Schedule background migrations for every module updated in this run.

        Scans each updated module's migrations/ directory for files matching
        background-*.py and enqueues a task for each one not yet done.
        Skipped on plain restarts (pool.updated_modules is empty).
        """
        if not self.pool.updated_modules:
            return
        for module_name in self.pool.updated_modules:
            self._schedule_module_migrations(module_name)

    @api.model
    def _schedule_module_migrations(self, module_name):
        """Scan module_name/migrations/ for background-*.py files."""
        module_path = get_module_path(module_name, display_warning=False)
        if not module_path:
            return

        migrations_dir = os.path.join(module_path, 'migrations')
        if not os.path.isdir(migrations_dir):
            return

        found = []
        for version_dir in sorted(os.listdir(migrations_dir)):
            if not _VERSION_RE.match(version_dir):
                continue
            pattern = os.path.join(
                migrations_dir, version_dir, 'background-*.py')
            for filepath in sorted(glob.glob(pattern)):
                filename = os.path.basename(filepath)
                # Strip 'background-' prefix and '.py' suffix
                migration_name = filename[len('background-'):-len('.py')]
                if not _NAME_RE.match(migration_name):
                    _logger.warning(
                        "Skipping migration %r: name %r contains "
                        "invalid characters.", filepath, migration_name)
                    continue
                found.append((version_dir, migration_name))

        if not found:
            return

        _logger.info(
            "Background migrations found for module %r: %s",
            module_name, ['%s/%s' % (v, n) for v, n in found])
        for version, migration_name in found:
            try:
                self._schedule_migration(module_name, version, migration_name)
            except Exception:
                _logger.error(
                    "Error scheduling migration %s/%s/%s",
                    module_name, version, migration_name, exc_info=True)

    @api.model
    def _schedule_migration(self, module, version, migration_name):
        """Ensure a record exists and enqueue a task if needed.

        - done → skip.
        - pending/failed → reset to pending and re-enqueue (deduped).
        - No record → create and enqueue.
        """
        existing = self.sudo().search([
            ('module', '=', module),
            ('module_version', '=', version),
            ('migration_name', '=', migration_name),
        ], limit=1)

        if existing:
            if existing.state == 'done':
                return
            old_state = existing.state
            existing.write({'state': 'pending'})
            existing._enqueue_task()
            _logger.info(
                "Re-scheduling migration %s/%s/%s (was: %s)",
                module, version, migration_name, old_state)
            return

        record = self.sudo().create({
            'name': '%s/%s/%s' % (module, version, migration_name),
            'module': module,
            'module_version': version,
            'migration_name': migration_name,
            'state': 'pending',
        })
        record._enqueue_task()
        _logger.info(
            "Scheduled new migration %s/%s/%s",
            module, version, migration_name)

    def action_force_reschedule(self):
        """Reset a failed migration to pending and re-enqueue its task."""
        for rec in self.sudo():
            if rec.state != 'failed':
                continue
            rec.write({'state': 'pending', 'date_completed': False})
            rec._enqueue_task()

    def _enqueue_task(self):
        unique_key = 'migration|%s|%s|%s' % (
            self.module, self.module_version, self.migration_name)
        task = self.env['generic.task.queue.task'].create_task(
            'task.type.background.migration',
            name='Migration: %s/%s/%s' % (
                self.module, self.module_version, self.migration_name),
            params={
                'module': self.module,
                'version': self.module_version,
                'name': self.migration_name,
            },
            unique_key=unique_key,
            on_conflict='reuse-running',
        )
        self.write({'task_id': task.id})
