"""Tests for GenericTaskQueueTask._gc_tasks (vacuum / cleanup cron).

Covers:
  - Tasks older than vacuum_days are deleted
  - Tasks newer than vacuum_days are kept
  - Both failed and cancelled terminal states are cleaned
  - Child tasks are removed via ON DELETE CASCADE when parent is deleted
  - batch size (vacuum_batch_size) caps records removed per run
  - vacuum_days = 0 disables cleanup entirely
  - Only terminal tasks are removed; pending tasks are safe
"""
from datetime import timedelta

from odoo import fields as odoo_fields
from odoo.tests.common import TransactionCase


class TestGcTasks(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.worker = self.env['generic.task.queue.worker'].create({
            'uuid': 'gc-test-worker',
            'service_name': 'test.service',
            'state': 'active',
        })
        self._set_param('generic_task_queue.vacuum_days', '30')
        self._set_param('generic_task_queue.vacuum_batch_size', '1000')

    def _set_param(self, key, value):
        self.env['ir.config_parameter'].sudo().set_param(key, value)

    def _make_old_done_task(self, days_ago=31, **extra_vals):
        """Create a done root task with date_completed backdated."""
        vals = {'name': 'Old done task', 'type_code': 'test.task.type.noop'}
        vals.update(extra_vals)
        task = self.Task.create(vals)
        task.sudo().action_assign(self.worker)
        task.sudo().action_start()
        task.sudo().action_done({'result': 'ok'})
        old_date = odoo_fields.Datetime.now() - timedelta(days=days_ago)
        task.sudo().write({'date_completed': old_date})
        return task

    # ------------------------------------------------------------------
    # Cutoff logic
    # ------------------------------------------------------------------

    def test_gc_deletes_old_done_tasks(self):
        """Tasks older than vacuum_days are deleted."""
        task = self._make_old_done_task(days_ago=31)
        task_id = task.id

        self.Task._gc_tasks()

        self.assertFalse(self.Task.browse(task_id).exists())

    def test_gc_keeps_recent_tasks(self):
        """Tasks newer than vacuum_days are not deleted."""
        task = self._make_old_done_task(days_ago=5)
        task_id = task.id

        self.Task._gc_tasks()

        self.assertTrue(self.Task.browse(task_id).exists())

    def test_gc_deletes_old_failed_and_cancelled_tasks(self):
        """Both failed and cancelled terminal tasks are vacuumed."""
        old_date = odoo_fields.Datetime.now() - timedelta(days=31)

        failed = self.Task.create({
            'name': 'Old failed',
            'type_code': 'test.task.type.noop',
        })
        failed.sudo().action_assign(self.worker)
        failed.sudo().action_start()
        failed.sudo().action_fail('simulated error')
        failed.sudo().write({'date_completed': old_date})

        cancelled = self.Task.create({
            'name': 'Old cancelled',
            'type_code': 'test.task.type.noop',
        })
        cancelled.action_cancel()
        cancelled.sudo().write({'date_completed': old_date})

        self.Task._gc_tasks()

        self.assertFalse(failed.exists())
        self.assertFalse(cancelled.exists())

    # ------------------------------------------------------------------
    # Cascade delete
    # ------------------------------------------------------------------

    def test_gc_cascades_to_children(self):
        """Deleting a root task removes its child tasks via CASCADE."""
        parent = self._make_old_done_task(days_ago=31)

        child = self.Task.create({
            'name': 'Child of old parent',
            'type_code': 'test.task.type.noop',
            'parent_id': parent.id,
        })
        child.sudo().action_assign(self.worker)
        child.sudo().action_start()
        child.sudo().action_done({'ok': True})
        # child.date_completed is recent, but parent is old enough to vacuum

        parent_id = parent.id
        child_id = child.id

        self.Task._gc_tasks()

        self.assertFalse(self.Task.browse(parent_id).exists())
        self.assertFalse(self.Task.browse(child_id).exists())

    # ------------------------------------------------------------------
    # vacuum_days = 0 disables cleanup
    # ------------------------------------------------------------------

    def test_gc_disabled_when_vacuum_days_zero(self):
        """vacuum_days=0 means never delete anything."""
        task = self._make_old_done_task(days_ago=365)
        task_id = task.id

        self._set_param('generic_task_queue.vacuum_days', '0')
        self.Task._gc_tasks()

        self.assertTrue(self.Task.browse(task_id).exists())

    # ------------------------------------------------------------------
    # Batch size cap
    # ------------------------------------------------------------------

    def test_gc_respects_batch_size(self):
        """At most vacuum_batch_size tasks are deleted per run."""
        for i in range(5):
            self._make_old_done_task(
                days_ago=31, name='Batch gc task %d' % i)

        # Cap batch size to 2
        self._set_param('generic_task_queue.vacuum_batch_size', '2')
        self.Task._gc_tasks()

        # Exactly 2 deleted, 3 remain
        remaining = self.Task.search([
            ('name', 'like', 'Batch gc task'),
            ('state', '=', 'done'),
        ])
        self.assertEqual(len(remaining), 3)

    # ------------------------------------------------------------------
    # Active tasks are not touched
    # ------------------------------------------------------------------

    def test_gc_skips_pending_tasks(self):
        """Pending tasks are never deleted regardless of age."""
        task = self.Task.create({
            'name': 'Pending task',
            'type_code': 'test.task.type.noop',
        })
        task_id = task.id

        # Backdate date_created so it looks very old
        old_date = odoo_fields.Datetime.now() - timedelta(days=365)
        task.sudo().write({'date_created': old_date})

        self._set_param('generic_task_queue.vacuum_days', '1')
        self.Task._gc_tasks()

        self.assertTrue(self.Task.browse(task_id).exists())
