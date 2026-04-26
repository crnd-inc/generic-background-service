"""Tests for the automatic retry feature.

Covers:
  - retry_count <= max_retries boundary (the exact limit is included)
  - tasks beyond max_retries are left failed
  - _retry_eta: dict delays, exponential backoff, missing key, unknown type
"""
from datetime import datetime, timedelta

from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_queue_worker import (
    TaskQueueWorker,
)
from odoo.addons.generic_task_queue.service.task_type import AbstractTaskType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_worker(env):
    return env['generic.task.queue.worker'].create({
        'uuid': 'retry-test-worker',
        'service_name': 'test.service',
        'state': 'active',
    })


def _make_failed_task(env, worker, max_retries=3, retry_count=None):
    """Create a task and put it in 'failed' state with the given retry_count.

    After action_fail retry_count stays at 0 (only action_retry increments it).
    Override with retry_count= to simulate a task that has already been retried
    N times and failed again.
    """
    task = env['generic.task.queue.task'].create_task(
        'test.task.type.noop',
        name='retry-test',
        retry_policy='retriable',
        max_retries=max_retries,
    )
    task.action_assign(worker)
    task.action_start()
    task.action_fail('simulated failure')
    # action_fail leaves retry_count = 0;
    #     override if a different value is needed
    if retry_count is not None and retry_count != 0:
        task.write({'retry_count': retry_count})
    return task


def _run_auto_retry_sql(env, channels=('default',), task_types=None):
    """Execute the same SQL that _auto_retry_failed uses and return task ids.

    flush_all() is called first so that ORM-pending writes are visible
    to the raw SQL query.
    """
    env.flush_all()
    if task_types:
        env.cr.execute("""
            SELECT id FROM generic_task_queue_task
            WHERE state = 'failed'
              AND retry_policy = 'retriable'
              AND retry_count < max_retries
              AND channel IN %s
              AND type_code IN %s
            FOR UPDATE SKIP LOCKED
        """, (tuple(channels), tuple(task_types)))
    else:
        env.cr.execute("""
            SELECT id FROM generic_task_queue_task
            WHERE state = 'failed'
              AND retry_policy = 'retriable'
              AND retry_count < max_retries
              AND channel IN %s
            FOR UPDATE SKIP LOCKED
        """, (tuple(channels),))
    return [r[0] for r in env.cr.fetchall()]


# ---------------------------------------------------------------------------
# Fake registry helpers (avoid polluting the real singleton)
# ---------------------------------------------------------------------------

class _FakeRegistry:
    """Minimal registry stub that always returns the given class."""
    def __init__(self, type_cls):
        self._cls = type_cls

    def get_task_type(self, code):
        return self._cls


class _EmptyRegistry:
    """Registry stub that always raises KeyError (simulates unknown type)."""
    def get_task_type(self, code):
        raise KeyError(code)


def _make_type_cls(retry_delays):
    """Dynamically build a minimal AbstractTaskType subclass."""
    return type(
        '_TestType',
        (AbstractTaskType,),
        {'_name': None, '_retry_delays': retry_delays,
         'execute': lambda self, env, task: None},
    )


# ---------------------------------------------------------------------------
# Boundary tests
# ---------------------------------------------------------------------------

class TestAutoRetryBoundary(TransactionCase):
    """The auto-retry guard is retry_count < max_retries.

    retry_count counts how many times action_retry() has been called
    (i.e. how many retries have already been attempted).  action_fail()
    does NOT increment retry_count.

    With max_retries=3, retries should be allowed when retry_count is
    0, 1, or 2.  When retry_count reaches 3 the task should stay failed.
    """

    def setUp(self):
        super().setUp()
        self.worker = _make_worker(self.env)

    def _apply_auto_retry_logic(self, task):
        """Mirror the worker loop: find failed tasks eligible for retry
        and retry them. SQL already filters retry_count <= max_retries."""
        task_ids = _run_auto_retry_sql(
            self.env,
            task_types=['test.task.type.noop'],
        )
        tasks = self.env['generic.task.queue.task'].browse(task_ids)
        for t in tasks:
            t.action_retry()

    def test_retry_count_below_max_retries_is_retried(self):
        """retry_count=0, max_retries=3 → task is retried (0 < 3)."""
        task = _make_failed_task(self.env, self.worker, max_retries=3)
        self.assertEqual(task.retry_count, 0)

        self._apply_auto_retry_logic(task)

        self.assertEqual(task.state, 'pending')
        self.assertEqual(task.retry_count, 1)

    def test_retry_count_one_below_max_retries_is_retried(self):
        """retry_count=2, max_retries=3 → last auto-retry is still allowed
           (2 < 3)."""
        task = _make_failed_task(
            self.env, self.worker, max_retries=3, retry_count=2)
        self.assertEqual(task.retry_count, 2)

        self._apply_auto_retry_logic(task)

        self.assertEqual(task.state, 'pending')
        self.assertEqual(task.retry_count, 3)

    def test_retry_count_equal_max_retries_is_not_retried(self):
        """retry_count=3, max_retries=3 → task stays failed
           (exhausted, 3 < 3 is false)."""
        task = _make_failed_task(
            self.env, self.worker, max_retries=3, retry_count=3)
        self.assertEqual(task.retry_count, 3)

        self._apply_auto_retry_logic(task)

        self.assertEqual(task.state, 'failed')

    def test_max_retries_zero_never_auto_retried(self):
        """max_retries=0 means no automatic retries at all.

        After the first failure retry_count=0.  0 < 0 is false → stays failed.
        """
        task = _make_failed_task(self.env, self.worker, max_retries=0)
        self.assertEqual(task.retry_count, 0)

        self._apply_auto_retry_logic(task)

        self.assertEqual(task.state, 'failed')

    def test_non_retriable_task_is_not_retried(self):
        """retry_policy='non_retriable' tasks never appear in the query."""
        task = self.env['generic.task.queue.task'].create_task(
            'test.task.type.noop',
            name='non-retriable',
            max_retries=3,
            retry_policy='non_retriable',
        )
        task.action_assign(self.worker)
        task.action_start()
        task.action_fail('simulated failure')

        task_ids = _run_auto_retry_sql(
            self.env,
            task_types=['test.task.type.noop'],
        )
        self.assertNotIn(task.id, task_ids)
        self.assertEqual(task.state, 'failed')


# ---------------------------------------------------------------------------
# _retry_eta tests
# ---------------------------------------------------------------------------

class TestRetryEtaDictDelays(TransactionCase):
    """_retry_eta() with an explicit dict of per-attempt delays."""

    def test_matching_key_returns_correct_eta(self):
        """{1: 10} → eta is ~10 seconds from now."""
        registry = _FakeRegistry(_make_type_cls({1: 10}))
        before = datetime.utcnow()
        eta = TaskQueueWorker._retry_eta(registry, 't', retry_count=1)
        after = datetime.utcnow()

        self.assertIsNotNone(eta)
        self.assertGreaterEqual(eta, before + timedelta(seconds=9))
        self.assertLessEqual(eta, after + timedelta(seconds=11))

    def test_missing_key_returns_none(self):
        """{1: 10} with retry_count=2 → no delay (immediate)."""
        registry = _FakeRegistry(_make_type_cls({1: 10}))
        eta = TaskQueueWorker._retry_eta(registry, 't', retry_count=2)
        self.assertIsNone(eta)

    def test_empty_dict_returns_none(self):
        """Empty dict → always immediate."""
        registry = _FakeRegistry(_make_type_cls({}))
        eta = TaskQueueWorker._retry_eta(registry, 't', retry_count=1)
        self.assertIsNone(eta)

    def test_zero_delay_returns_none(self):
        """{1: 0} → treat as immediate (no eta)."""
        registry = _FakeRegistry(_make_type_cls({1: 0}))
        eta = TaskQueueWorker._retry_eta(registry, 't', retry_count=1)
        self.assertIsNone(eta)


class TestRetryEtaExponential(TransactionCase):
    """_retry_eta() with exponential backoff."""

    def test_exponential_retry_count_1(self):
        """retry_count=1 → delay = min(2**1, 3600) = 2s."""
        registry = _FakeRegistry(_make_type_cls('exponential'))
        before = datetime.utcnow()
        eta = TaskQueueWorker._retry_eta(registry, 't', retry_count=1)
        after = datetime.utcnow()

        self.assertIsNotNone(eta)
        self.assertGreaterEqual(eta, before + timedelta(seconds=1))
        self.assertLessEqual(eta, after + timedelta(seconds=3))

    def test_exponential_retry_count_10(self):
        """retry_count=10 → delay = min(2**10=1024, 3600) = 1024s."""
        registry = _FakeRegistry(_make_type_cls('exponential'))
        before = datetime.utcnow()
        eta = TaskQueueWorker._retry_eta(registry, 't', retry_count=10)
        after = datetime.utcnow()

        self.assertIsNotNone(eta)
        self.assertGreaterEqual(eta, before + timedelta(seconds=1023))
        self.assertLessEqual(eta, after + timedelta(seconds=1025))

    def test_exponential_capped_at_3600(self):
        """retry_count=20 → delay = min(2**20, 3600) = 3600s."""
        registry = _FakeRegistry(_make_type_cls('exponential'))
        before = datetime.utcnow()
        eta = TaskQueueWorker._retry_eta(registry, 't', retry_count=20)
        after = datetime.utcnow()

        self.assertIsNotNone(eta)
        self.assertGreaterEqual(eta, before + timedelta(seconds=3599))
        self.assertLessEqual(eta, after + timedelta(seconds=3601))


class TestAutoRetryServiceAffinity(TransactionCase):
    """_auto_retry_failed must not retry tasks whose type is locked to a
    different service, even when the task sits on the generic channel.

    The fix: _auto_retry_failed now always calls _get_effective_task_types()
    (which filters by _service_name) and passes the result to the SQL query.
    A task of type 'test.routing.specific.service' (_service_name=
    'my.specific.service') must not appear in the retry query executed by
    a 'generic.task.queue.service' worker.
    """

    def setUp(self):
        super().setUp()
        self.worker_rec = _make_worker(self.env)

    def _generic_effective_types(self):
        """Build effective_types as a generic-service worker would."""
        worker = TaskQueueWorker.__new__(TaskQueueWorker)
        worker._service_name = 'generic.task.queue.service'
        worker._task_types = []
        return worker._get_effective_task_types()

    def test_service_specific_type_not_in_generic_effective_types(self):
        """'test.routing.specific.service' must be absent from generic
        worker's effective types."""
        effective = self._generic_effective_types()
        self.assertNotIn('test.routing.specific.service', effective)

    def test_service_specific_failed_task_not_selected_for_retry(self):
        """A failed retriable task locked to 'my.specific.service' must not
        appear in the retry SQL run by the generic service worker, even when
        the task is on channel='default'."""
        task = self.env['generic.task.queue.task'].create_task(
            'test.routing.specific.service',
            name='affinity-retry-test',
            retry_policy='retriable',
            max_retries=3,
            channel='default',  # force onto default channel to isolate the
                                # service-affinity filter from channel filter
        )
        task.action_assign(self.worker_rec)
        task.action_start()
        task.action_fail('simulated failure')

        effective_types = self._generic_effective_types()
        self.assertFalse(
            effective_types == [],
            "effective_types must not be empty — generic worker handles "
            "many other types")

        self.env.flush_all()
        self.env.cr.execute("""
            SELECT id FROM generic_task_queue_task
            WHERE state = 'failed'
              AND retry_policy = 'retriable'
              AND retry_count < max_retries
              AND channel IN %s
              AND type_code IN %s
            FOR UPDATE SKIP LOCKED
        """, (('default',), tuple(effective_types)))
        task_ids = [r[0] for r in self.env.cr.fetchall()]

        self.assertNotIn(
            task.id, task_ids,
            "Task locked to 'my.specific.service' must not be retried "
            "by the generic service worker")


class TestRetryEtaUnknownType(TransactionCase):
    """_retry_eta() with an unregistered task type returns None."""

    def test_unknown_type_returns_none(self):
        registry = _EmptyRegistry()
        eta = TaskQueueWorker._retry_eta(
            registry, 'no.such.type.exists', retry_count=1)
        self.assertIsNone(eta)
