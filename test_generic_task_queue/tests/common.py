import contextlib

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


def recover_dead_worker(env, worker_rec):
    """Mark a worker record dead and sweep its tasks, as a peer's stale
    check would."""
    worker_rec.mark_dead()
    make_service_worker(env)._recover_dead_worker_tasks()
