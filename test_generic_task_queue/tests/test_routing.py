"""Tests for _service_name affinity and _default_channel on AbstractTaskType.

Covers:
  - create_task() resolves channel from _default_channel when none given
  - explicit channel overrides _default_channel
  - _get_effective_task_types() includes types with _service_name=None
  - _get_effective_task_types() includes types matching the service name
  - _get_effective_task_types() excludes types belonging to another service
  - _task_types allowlist further restricts effective types
  - service_name and default_channel are synced to DB record

Task type stubs used here are defined in
test_generic_task_queue/service/test_task_types.py so they are synced
to the DB during module install and available to create_task().
"""
from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_queue_worker import (
    TaskQueueWorker,
)


# ---------------------------------------------------------------------------
# create_task() channel resolution
# ---------------------------------------------------------------------------

class TestDefaultChannel(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']

    def test_no_channel_uses_default_channel_from_type(self):
        """create_task() without channel uses the type's _default_channel."""
        task = self.Task.create_task('test.routing.custom.channel')
        self.assertEqual(task.channel, 'heavy')

    def test_explicit_channel_overrides_default(self):
        """Explicitly passing channel overrides _default_channel."""
        task = self.Task.create_task(
            'test.routing.custom.channel', channel='urgent')
        self.assertEqual(task.channel, 'urgent')

    def test_noop_type_uses_default_channel(self):
        """test.task.type.noop has _default_channel='default'."""
        task = self.Task.create_task('test.task.type.noop')
        self.assertEqual(task.channel, 'default')


# ---------------------------------------------------------------------------
# _get_effective_task_types() service affinity filtering
# ---------------------------------------------------------------------------

def _make_worker(service_name, task_types=None):
    """Build a minimal TaskQueueWorker with mocked infrastructure."""
    worker = TaskQueueWorker.__new__(TaskQueueWorker)
    worker._service_name = service_name
    worker._task_types = task_types or []
    return worker


class TestEffectiveTaskTypes(TransactionCase):

    def test_includes_types_with_no_service_name(self):
        """Types with _service_name=None are claimable by any worker."""
        worker = _make_worker('my.specific.service')
        effective = worker._get_effective_task_types()
        self.assertIn('test.routing.any.service', effective)

    def test_includes_types_matching_service_name(self):
        """Types whose _service_name matches the worker are included."""
        worker = _make_worker('my.specific.service')
        effective = worker._get_effective_task_types()
        self.assertIn('test.routing.specific.service', effective)

    def test_excludes_types_belonging_to_other_service(self):
        """Types with a different _service_name are excluded."""
        worker = _make_worker('my.specific.service')
        effective = worker._get_effective_task_types()
        self.assertNotIn('test.routing.other.service', effective)

    def test_task_types_allowlist_restricts_further(self):
        """_task_types allowlist further narrows effective types."""
        worker = _make_worker(
            'my.specific.service',
            task_types=['test.routing.any.service'])
        effective = worker._get_effective_task_types()
        self.assertIn('test.routing.any.service', effective)
        self.assertNotIn('test.routing.specific.service', effective)

    def test_default_service_excludes_service_specific_types(self):
        """Default service does not claim types locked to another service."""
        worker = _make_worker('generic.task.queue.service')
        effective = worker._get_effective_task_types()
        self.assertNotIn('test.routing.specific.service', effective)
        self.assertNotIn('test.routing.other.service', effective)
        self.assertIn('test.routing.any.service', effective)


# ---------------------------------------------------------------------------
# DB sync
# ---------------------------------------------------------------------------

class TestTaskTypeDbSync(TransactionCase):

    def test_service_name_synced_to_db(self):
        """service_name is written to the task type DB record on sync."""
        rec = self.env['generic.task.queue.task.type'].search(
            [('code', '=', 'test.routing.specific.service')])
        if not rec:
            self.skipTest('task type not yet synced — run with module install')
        self.assertEqual(rec.service_name, 'my.specific.service')

    def test_default_channel_synced_to_db(self):
        """default_channel is written to the task type DB record on sync."""
        rec = self.env['generic.task.queue.task.type'].search(
            [('code', '=', 'test.routing.custom.channel')])
        if not rec:
            self.skipTest('task type not yet synced — run with module install')
        self.assertEqual(rec.default_channel, 'heavy')
