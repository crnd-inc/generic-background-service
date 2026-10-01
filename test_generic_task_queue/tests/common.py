import contextlib
from unittest.mock import patch

from odoo.addons.generic_task_queue.service.task_queue_worker import (
    TaskQueueWorker,
)


def make_service_worker(env):
    """A TaskQueueWorker with no thread or DB connection: with_env() yields
    the test env, so its recovery paths run in the test transaction."""
    worker = TaskQueueWorker.__new__(TaskQueueWorker)
    worker._active_tasks = []
    worker._last_stale_check = float('-inf')
    worker.with_env = contextlib.contextmanager(lambda: iter([env]))
    return worker


def make_worker(env, uuid, **vals):
    """An active worker record of the test service."""
    return env['generic.task.queue.worker'].create(dict(
        {'uuid': uuid, 'service_name': 'test.svc', 'state': 'active'},
        **vals))


def make_task(env, worker=None, state='running', runner_id=None, **vals):
    """A noop task; with a worker, claimed by it and brought to ``state``
    (assigned / running / stuck), optionally with ``runner_id`` stamped."""
    task = env['generic.task.queue.task'].create(dict(
        {'name': 'Task', 'type_code': 'test.task.type.noop'}, **vals))
    if worker is not None:
        task.action_assign(worker)
        if state in ('running', 'stuck'):
            task.action_start()
        if state == 'stuck':
            task.action_stuck()
        if runner_id:
            task.sudo().write({'runner_id': runner_id})
    return task


def record_on_failure(test, task_type_cls):
    """Patch task_type_cls.on_failure for the calling test; returns a list
    filled with the type of each ``exc`` it receives (the type only, so no
    traceback keeps a failed transaction's env alive)."""
    seen = []

    def _on_failure(self, env, task, exc):
        seen.append(type(exc))

    patcher = patch.object(task_type_cls, 'on_failure', _on_failure)
    patcher.start()
    test.addCleanup(patcher.stop)
    return seen


def recover_dead_worker(env, worker_rec):
    """Mark a worker record dead and sweep its tasks, as a peer's stale
    check would."""
    worker_rec.mark_dead()
    make_service_worker(env)._recover_dead_worker_tasks()
