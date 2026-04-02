import logging
import time
import traceback
import threading
import uuid

from odoo import exceptions as odoo_exceptions

from odoo.addons.generic_background_service import (
    AbstractBackgroundServiceWorker,
)
from .task_type_registry import TaskTypeRegistry

_logger = logging.getLogger(__name__)

# How often (in seconds) to check for stale peer workers
STALE_CHECK_INTERVAL = 60


class _TaskThread:
    """ Tracks a running task thread.
    """
    __slots__ = ('task_id', 'thread', 'start_time', 'timeout')

    def __init__(self, task_id, thread, timeout=0):
        self.task_id = task_id
        self.thread = thread
        self.start_time = time.monotonic()
        self.timeout = timeout


class TaskQueueWorker(AbstractBackgroundServiceWorker):
    """ Worker that polls the task queue and executes tasks
        in separate threads.

        The worker's run_service() loop acts as a supervisor:
        - Sends heartbeat
        - Checks for completed/timed-out task threads
        - Claims new tasks if free slots are available
        - Spawns task threads for claimed tasks

        Task execution happens in _TaskThread threads,
        not in the worker's own thread. This ensures the
        worker can always heartbeat and respond to stop signals.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._worker_uuid = str(uuid.uuid4())
        self._channels = self._worker_params.get(
            'channels', ['default'])
        self._task_types = self._worker_params.get(
            'task_types', [])
        self._max_parallel_jobs = self._worker_params.get(
            'max_parallel_jobs', 1)

        # Active task threads: list of _TaskThread
        self._active_tasks = []

        # Worker record ID in the database
        self._worker_record_id = None

        # Timestamp of last stale-worker check
        self._last_stale_check = 0

    def get_sleep_timeout(self):
        if self._active_tasks:
            # Tasks running — poll frequently to detect completion
            return 0.5
        # No tasks — poll less aggressively
        return 1.0

    def on_init(self):
        with self.with_env() as env:
            worker_rec = env['generic.task.queue.worker'].find_or_create(
                service_name=self._worker_service_name,
                dbname=self._worker_dbname,
                uuid=self._worker_uuid,
                channels=','.join(self._channels),
                task_types=','.join(self._task_types),
                max_parallel_jobs=self._max_parallel_jobs,
            )
            self._worker_record_id = worker_rec.id

    def on_shutdown(self):
        # Wait for running task threads to finish
        self._wait_for_task_threads(timeout=10)

        # Mark worker as dead, reassign stuck tasks
        try:
            with self.with_env() as env:
                worker = env['generic.task.queue.worker'].browse(
                    self._worker_record_id)
                if worker.exists():
                    worker.mark_dead()
        except Exception:
            _logger.error(
                "Error marking worker %s as dead",
                self._worker_uuid, exc_info=True)

    def run_service(self):
        # 1. Heartbeat
        self._do_heartbeat()

        # 2. Finalize completed task threads
        self._finalize_completed()

        # 3. Check for timed-out task threads
        self._check_timeouts()

        # 4. Check waiting parents
        self._check_waiting_parents()

        # 5. Auto-retry failed retriable tasks
        self._auto_retry_failed()

        # 6. Periodically check for stale peer workers
        self._check_stale_peers()

        # 7. Claim and spawn new tasks if free slots
        free_slots = self._max_parallel_jobs - len(self._active_tasks)
        if free_slots > 0 and self._task_types:
            self._claim_and_spawn(free_slots)

    def _do_heartbeat(self):
        try:
            with self.with_env() as env:
                worker = env['generic.task.queue.worker'].browse(
                    self._worker_record_id)
                worker.heartbeat()
        except Exception:
            _logger.error(
                "Error sending heartbeat for worker %s",
                self._worker_uuid, exc_info=True)

    def _claim_and_spawn(self, limit):
        task_ids = []
        try:
            with self.with_env() as env:
                worker = env['generic.task.queue.worker'].browse(
                    self._worker_record_id)
                Task = env['generic.task.queue.task']
                tasks = Task.claim_task(
                    worker, self._channels, self._task_types,
                    limit=limit)
                # Read task data before leaving the transaction
                task_ids = [
                    (t.id, t.timeout) for t in tasks
                ]
        except Exception:
            _logger.error(
                "Error claiming tasks for worker %s",
                self._worker_uuid, exc_info=True)
            return

        for task_id, timeout in task_ids:
            self._spawn_task_thread(task_id, timeout)

    def _spawn_task_thread(self, task_id, timeout=0):
        thread = threading.Thread(
            target=self._task_thread_target,
            args=(task_id,),
            name="TaskExec-%s-%d" % (self._worker_uuid[:8], task_id),
            daemon=True,
        )
        task_info = _TaskThread(task_id, thread, timeout)
        self._active_tasks.append(task_info)
        thread.start()

    def _task_thread_target(self, task_id):
        """ Runs in a separate thread. Executes one task. """
        # Phase 1: mark as running
        try:
            with self.with_env() as env:
                task = env['generic.task.queue.task'].browse(task_id)
                task.action_start()
        except Exception:
            _logger.error(
                "Failed to start task %d", task_id, exc_info=True)
            return

        # Phase 2: execute in the context of the creating user
        try:
            with self.with_env() as env:
                task = env['generic.task.queue.task'].browse(task_id)
                registry = TaskTypeRegistry()
                task_type_cls = registry.get_task_type(task.type_code)
                task_type = task_type_cls()

                # Switch to creating user's context so access
                # rules apply. Task types that need superuser
                # access can explicitly call sudo().
                user_env = env(user=task.create_uid.id)
                result = task_type.execute(user_env, task)

                # Call on_success hook
                try:
                    task_type.on_success(env, task, result)
                except Exception:
                    _logger.error(
                        "Error in on_success hook for task %d",
                        task_id, exc_info=True)

                try:
                    task.action_done(result)
                    # Notify parent if this is a child task
                    self._notify_parent_on_child_done(env, task)
                except odoo_exceptions.ValidationError:
                    # Task was already transitioned (timed out or
                    # cancelled) by the worker thread — that's OK
                    _logger.info(
                        "Task %d already transitioned "
                        "(likely timed out or cancelled)",
                        task_id)
        except Exception as exc:
            _logger.error(
                "Task %d failed", task_id, exc_info=True)
            try:
                with self.with_env() as env:
                    task = env['generic.task.queue.task'].browse(task_id)

                    # Call on_failure hook
                    registry = TaskTypeRegistry()
                    task_type_cls = registry.get_task_type(
                        task.type_code)
                    task_type = task_type_cls()
                    try:
                        task_type.on_failure(env, task, exc)
                    except Exception:
                        _logger.error(
                            "Error in on_failure hook for task %d",
                            task_id, exc_info=True)

                    try:
                        task.action_fail(traceback.format_exc())
                    except odoo_exceptions.ValidationError:
                        _logger.info(
                            "Task %d already transitioned "
                            "(likely timed out or cancelled)",
                            task_id)
            except Exception:
                _logger.error(
                    "Failed to mark task %d as failed",
                    task_id, exc_info=True)

    def _finalize_completed(self):
        """ Remove completed task threads from active list. """
        still_active = []
        for task_info in self._active_tasks:
            if task_info.thread.is_alive():
                still_active.append(task_info)
        self._active_tasks = still_active

    def _check_timeouts(self):
        """ Check for task threads that exceeded their timeout. """
        now = time.monotonic()
        for task_info in self._active_tasks:
            if task_info.timeout <= 0:
                continue
            elapsed = now - task_info.start_time
            if elapsed > task_info.timeout:
                _logger.warning(
                    "Task %d timed out after %.1f seconds "
                    "(timeout: %d)",
                    task_info.task_id, elapsed, task_info.timeout)
                self._timeout_task(task_info)

    def _timeout_task(self, task_info):
        """ Mark a timed-out task as failed.

            Re-reads state from DB to handle the race where
            the task thread completed between the timeout check
            and this method.
        """
        try:
            with self.with_env() as env:
                task = env['generic.task.queue.task'].browse(
                    task_info.task_id)
                # Re-read from DB to see if task thread
                # already finished
                task.invalidate_recordset()
                if task.state == 'running':
                    try:
                        task.action_fail(
                            "Execution timed out after %d seconds"
                            % task_info.timeout)
                    except odoo_exceptions.ValidationError:
                        # Task thread finished between our read
                        # and write — that's OK
                        _logger.debug(
                            "Task %d already transitioned "
                            "(race with timeout)",
                            task_info.task_id)
        except Exception:
            _logger.error(
                "Error marking task %d as timed out",
                task_info.task_id, exc_info=True)
        # Mark timeout so we don't re-check
        task_info.timeout = 0

    def _check_waiting_parents(self):
        """ Check waiting parent tasks and complete them
            if all children are done. """
        try:
            with self.with_env() as env:
                Task = env['generic.task.queue.task']
                waiting = Task.search([
                    ('state', '=', 'waiting'),
                    ('worker_id.id', '=', self._worker_record_id),
                ], limit=10)
                for task in waiting:
                    task._check_waiting_parent()
        except Exception:
            _logger.error(
                "Error checking waiting parents", exc_info=True)

    def _notify_parent_on_child_done(self, env, child_task):
        """ Notify parent task that a child has completed.

            Calls on_child_done hook on the parent's task type.
        """
        if not child_task.parent_id:
            return
        parent = child_task.parent_id
        if parent.state != 'waiting':
            return
        try:
            registry = TaskTypeRegistry()
            task_type_cls = registry.get_task_type(parent.type_code)
            task_type = task_type_cls()
            task_type.on_child_done(env, parent, child_task)
        except Exception:
            _logger.error(
                "Error in on_child_done for parent %d",
                parent.id, exc_info=True)

    def _auto_retry_failed(self):
        """ Automatically retry failed retriable tasks.

            Uses FOR UPDATE SKIP LOCKED to prevent multiple
            workers from retrying the same tasks simultaneously.
        """
        if not self._channels or not self._task_types:
            return
        try:
            with self.with_env() as env:
                env.cr.execute("""
                    SELECT id FROM generic_task_queue_task
                    WHERE state = 'failed'
                      AND retry_policy = 'retriable'
                      AND channel IN %s
                      AND type_code IN %s
                    LIMIT 10
                    FOR UPDATE SKIP LOCKED
                """, (tuple(self._channels), tuple(self._task_types)))
                task_ids = [r[0] for r in env.cr.fetchall()]
                if task_ids:
                    tasks = env['generic.task.queue.task'].browse(task_ids)
                    for task in tasks:
                        if task.retry_count < task.max_retries:
                            task.action_retry()
        except Exception:
            _logger.error(
                "Error auto-retrying failed tasks", exc_info=True)

    def _check_stale_peers(self):
        """ Periodically check for stale peer workers. """
        now = time.monotonic()
        if now - self._last_stale_check < STALE_CHECK_INTERVAL:
            return
        self._last_stale_check = now
        try:
            with self.with_env() as env:
                env['generic.task.queue.worker'].check_stale_workers()
        except Exception:
            _logger.error(
                "Error checking stale workers", exc_info=True)

    def _wait_for_task_threads(self, timeout=10):
        """ Wait for running task threads to finish. """
        deadline = time.monotonic() + timeout
        for task_info in self._active_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if task_info.thread.is_alive():
                task_info.thread.join(timeout=remaining)
                if task_info.thread.is_alive():
                    _logger.warning(
                        "Task thread %d still alive after shutdown wait",
                        task_info.task_id)
        self._active_tasks.clear()
