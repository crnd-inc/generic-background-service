import logging
import socket
import time
import traceback
import threading
import uuid
from datetime import datetime, timedelta

import psycopg2.errors

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
    __slots__ = ('task_id', 'thread', 'start_time', 'timeout',
                 'timed_out', 'runner_id')

    def __init__(self, task_id, thread, timeout=0, runner_id=None):
        self.task_id = task_id
        self.thread = thread
        self.start_time = time.monotonic()
        self.timeout = timeout
        self.timed_out = False      # Set True after timeout fires
        self.runner_id = runner_id  # Captured at claim time


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

        Stuck task handling
        -------------------
        When a task thread exceeds its timeout but is still alive,
        the task is marked 'stuck'. Stuck threads continue to occupy
        their parallel slot. is_stuck() returns True when ALL slots are
        occupied by timed-out live threads (worker cannot claim new tasks).

        The service (BackgroundService._check_stuck()) observes is_stuck()
        and decides what to do based on execution mode and the service's
        _die_on_stuck_timeout class attribute:
          - Worker mode (prefork): service stops → process dies → Odoo
            respawns → _cleanup_orphaned_tasks() recovers stuck tasks.
          - Threaded mode: log only; service and worker keep running;
            natural self-healing when stuck threads eventually finish.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._worker_uuid = str(uuid.uuid4())
        self._service_name = self._worker_params.get('service_name')
        self._channels = self._worker_params.get(
            'channels', ['default'])
        self._task_types = self._worker_params.get(
            'task_types', [])
        self._max_parallel_jobs = self._worker_params.get(
            'max_parallel_jobs', 1)
        self._default_task_timeout = self._worker_params.get(
            'default_task_timeout', 0)

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

    def is_stuck(self) -> bool:
        """True when all parallel slots are occupied by timed-out threads.

        The worker cannot claim new tasks at this point. The service's
        beat loop observes this and decides whether to intervene based
        on execution mode and _die_on_stuck_timeout.
        """
        if not self._active_tasks:
            return False
        stuck_count = sum(
            1 for t in self._active_tasks
            if t.timed_out and t.thread.is_alive()
        )
        return stuck_count >= self._max_parallel_jobs

    def on_init(self):
        with self.with_env() as env:
            worker_rec = env['generic.task.queue.worker'].find_or_create(
                service_name=self._worker_service_name,
                dbname=self._worker_dbname,
                uuid=self._worker_uuid,
                channels=','.join(self._channels),
                task_types=','.join(self._task_types),
                max_parallel_jobs=self._max_parallel_jobs,
                hostname=socket.gethostname(),
            )
            self._worker_record_id = worker_rec.id
        # Cleanup orphaned tasks in a separate transaction so it
        # commits independently of find_or_create.
        self._cleanup_orphaned_tasks(self._worker_record_id)

    def on_shutdown(self):
        # Wait briefly for task threads to finish cleanly.
        # Threads that complete in time will have already written their
        # own final state (done/failed) — nothing more to do for them.
        self._wait_for_task_threads(timeout=10)

        # Do NOT call mark_dead() here.
        #
        # Any thread still alive after the wait (running or stuck) has
        # already started executing business logic — reassigning its task
        # now would cause two threads to run the same task concurrently,
        # potentially deadlocking on DB row locks or duplicate external
        # API calls. The runner_id guard only prevents the final state
        # write from being duplicated; it cannot stop the concurrent work.
        #
        # Task reassignment is handled safely by two other paths:
        #
        #   1. Next startup (worker mode / clean respawn):
        #      find_or_create() reuses this worker record, then
        #      _cleanup_orphaned_tasks() reassigns assigned/running/stuck
        #      tasks. By this point the old process — and all its threads —
        #      are dead. Safe.
        #
        #   2. Stale detection (unclean death — SIGKILL, OOM):
        #      Heartbeat stops. After DEFAULT_HEARTBEAT_TIMEOUT (60s) a
        #      peer calls check_stale_workers() → mark_dead(). By then
        #      the process has been dead long enough for all threads to be
        #      gone. Safe.
        _logger.info(
            "Worker %s shut down. In-flight tasks will be reassigned "
            "on next startup or by stale detection.",
            self._worker_uuid)

    def run_service(self):
        # 1. Heartbeat
        self._do_heartbeat()

        # 2. Finalize completed task threads
        self._finalize_completed()

        # 3. Check for timed-out task threads → mark stuck
        self._check_timeouts()

        # 4. Check waiting parents
        self._check_waiting_parents()

        # 5. Auto-retry failed retriable tasks
        self._auto_retry_failed()

        # 6. Periodically check for stale peer workers
        self._check_stale_peers()

        # 7. Claim and spawn new tasks if free slots
        free_slots = self._max_parallel_jobs - len(self._active_tasks)
        if free_slots > 0:
            self._claim_and_spawn(free_slots)

    def _do_heartbeat(self):
        try:
            with self.with_env() as env:
                worker = env['generic.task.queue.worker'].browse(
                    self._worker_record_id)
                worker.heartbeat(stuck=self.is_stuck())
        except Exception:
            _logger.error(
                "Error sending heartbeat for worker %s",
                self._worker_uuid, exc_info=True)

    def _get_effective_task_types(self):
        """ Return type codes this worker may claim.

            Filters by service affinity (_service_name) and the optional
            declared _task_types allowlist. Re-evaluated on every claim
            cycle so late-registered types are picked up automatically.

            A task type is claimable when:
            - its _service_name is None (any service), OR
            - its _service_name matches this worker's service name.
            Then further restricted to _task_types if that list is non-empty.
        """
        all_types = TaskTypeRegistry.get_initialized_types()
        effective = [
            name for name, cls in all_types.items()
            if getattr(cls, '_service_name', None) in (
                None, self._service_name)
        ]
        if self._task_types:
            allowed = frozenset(self._task_types)
            effective = [t for t in effective if t in allowed]
        return effective

    @staticmethod
    def _get_singleton_types():
        """ Return set of type codes that have _singleton = True. """
        return frozenset(
            name
            for name, cls in TaskTypeRegistry.get_initialized_types().items()
            if getattr(cls, '_singleton', False)
        )

    def _claim_and_spawn(self, limit):
        task_data = []
        try:
            with self.with_env() as env:
                worker = env['generic.task.queue.worker'].browse(
                    self._worker_record_id)
                Task = env['generic.task.queue.task']
                tasks = Task.claim_task(
                    worker, self._channels,
                    self._get_effective_task_types(),
                    singleton_types=self._get_singleton_types(),
                    limit=limit)
                # Read all task data before leaving the transaction.
                # Resolve timeout here: task → type default → worker default.
                for t in tasks:
                    task_timeout = t.timeout or 0
                    type_timeout = (t.type_id.default_timeout or 0
                                    if t.type_id else 0)
                    resolved_timeout = (
                        task_timeout if task_timeout > 0
                        else type_timeout if type_timeout > 0
                        else self._default_task_timeout
                    )
                    task_data.append((t.id, resolved_timeout, t.runner_id))
        except Exception:
            _logger.error(
                "Error claiming tasks for worker %s",
                self._worker_uuid, exc_info=True)
            return

        for task_id, timeout, runner_id in task_data:
            self._spawn_task_thread(task_id, timeout, runner_id)

    def _spawn_task_thread(self, task_id, timeout=0, runner_id=None):
        thread = threading.Thread(
            target=self._task_thread_target,
            args=(task_id, runner_id),
            name="TaskExec-%s-%d" % (self._worker_uuid[:8], task_id),
            daemon=True,
        )
        task_info = _TaskThread(task_id, thread, timeout, runner_id)
        self._active_tasks.append(task_info)
        thread.start()

    def _task_thread_target(self, task_id, runner_id=None):
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
        _notify_parent = False
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

                # If execute() called action_wait_children(),
                # the task is now in 'waiting' state — don't
                # call action_done. It will be completed later
                # by _check_waiting_parent when all children finish.
                task.invalidate_recordset(['state'])
                if task.state == 'waiting':
                    return

                # Call on_success hook
                try:
                    task_type.on_success(env, task, result)
                except Exception:
                    _logger.error(
                        "Error in on_success hook for task %d",
                        task_id, exc_info=True)

                try:
                    task.action_done(result, runner_id=runner_id)
                    # Record whether parent notification is needed.
                    # The actual call happens after this with-block
                    # commits so on_child_done runs in a fresh
                    # transaction with no row locks held.
                    _notify_parent = bool(task.parent_id)
                except odoo_exceptions.ValidationError:
                    # Task was already transitioned (timed out/stuck or
                    # cancelled) by the worker — that's OK.
                    _logger.info(
                        "Task %d already transitioned "
                        "(likely timed out/stuck or cancelled)",
                        task_id)
            # env.cr commits here — child row lock fully released
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
                        task.action_fail(
                            traceback.format_exc(), runner_id=runner_id)
                    except odoo_exceptions.ValidationError:
                        _logger.info(
                            "Task %d already transitioned "
                            "(likely timed out/stuck or cancelled)",
                            task_id)
            except Exception:
                _logger.error(
                    "Failed to mark task %d as failed",
                    task_id, exc_info=True)

        # Phase 3: notify parent in a fresh transaction.
        # Runs AFTER Phase 2 commits so no child row lock is held.
        # This prevents deadlocks when on_child_done calls
        # update_progress() (which opens a separate cursor).
        if _notify_parent:
            self._notify_parent_on_child_done(task_id)

    def _finalize_completed(self):
        """ Remove completed task threads from active list.

            For threads that timed out and are now done, verify the task
            was properly finalized. If it is still 'stuck' (the thread
            exited without calling action_done/action_fail), apply retry
            policy so the task does not stay in permanent limbo.
        """
        still_active = []
        resolved_stuck = []
        for task_info in self._active_tasks:
            if task_info.thread.is_alive():
                still_active.append(task_info)
            elif task_info.timed_out:
                resolved_stuck.append(task_info)
        self._active_tasks = still_active
        if resolved_stuck:
            self._handle_resolved_stuck_threads(resolved_stuck)

    def _handle_resolved_stuck_threads(self, resolved_stuck):
        """ For timed-out threads that are now finished, check whether
            their task is still 'stuck'. If so, the thread exited without
            writing a final state — apply retry policy to prevent the task
            from staying in permanent limbo.

            This is a safety net for the uncommon case where a task thread
            terminates without calling action_done() or action_fail().
        """
        for task_info in resolved_stuck:
            try:
                with self.with_env() as env:
                    task = env['generic.task.queue.task'].browse(
                        task_info.task_id)
                    task.invalidate_recordset(['state', 'runner_id'])
                    if task.state != 'stuck':
                        # Thread wrote its own final state — nothing to do
                        continue
                    if task.runner_id != task_info.runner_id:
                        # Task was reassigned while stuck — zombie guard
                        # already handles it
                        continue
                    _logger.warning(
                        "Task %d stuck thread resolved without writing "
                        "final state; applying retry policy "
                        "(retry_policy=%s, retry_count=%d)",
                        task_info.task_id,
                        task.retry_policy, task.retry_count)
                    if task.retry_policy == 'retriable':
                        task.write({
                            'state': 'pending',
                            'worker_id': False,
                            'runner_id': False,
                            'task_error': False,
                            'progress': 0,
                        })
                    else:
                        task.write({
                            'state': 'failed',
                            'task_error': (
                                'Task thread exited without result '
                                'after timeout'),
                        })
            except Exception:
                _logger.error(
                    "Error finalizing resolved stuck thread "
                    "for task %d",
                    task_info.task_id, exc_info=True)

    def _check_timeouts(self):
        """ Check for task threads that exceeded their timeout.

            If a thread is still alive after timeout: mark task stuck.
            If a thread has already finished: nothing to do (the thread
            called action_done/fail itself).
        """
        now = time.monotonic()
        for task_info in self._active_tasks:
            if task_info.timed_out:
                continue   # Already processed — don't re-check
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
        """ Handle a task thread that exceeded its timeout.

            If thread is still alive: mark task 'stuck' (outcome unknown).
            If thread already finished: nothing to do (it handled itself).
            Always sets task_info.timed_out = True to prevent re-processing.
        """
        try:
            with self.with_env() as env:
                task = env['generic.task.queue.task'].browse(
                    task_info.task_id)
                task.invalidate_recordset(['state'])
                if task_info.thread.is_alive():
                    if task.state == 'running':
                        try:
                            task.action_stuck()
                        except odoo_exceptions.ValidationError:
                            # Thread completed between our read and write
                            _logger.debug(
                                "Task %d already transitioned "
                                "(race with timeout)",
                                task_info.task_id)
                # If thread is NOT alive, it already called
                # action_done/action_fail — nothing to do here.
        except Exception:
            _logger.error(
                "Error handling timeout for task %d",
                task_info.task_id, exc_info=True)
        # Always mark timed_out so _check_timeouts doesn't re-enter
        task_info.timed_out = True

    def _cleanup_orphaned_tasks(self, worker_record_id):
        """ On startup, any task owned by this worker record that is
            still in an active state is orphaned (previous crash with
            no clean shutdown). Apply retry policy immediately, before
            claiming any new tasks.

            In worker mode this covers the case where the process was
            killed (SIGKILL, OOM) without on_shutdown() running.

            'waiting' tasks: the parent task was waiting for children when
            the worker died. We only clear worker_id so _check_waiting_parents
            can re-evaluate on the first poll cycle. We do NOT re-execute the
            task from scratch — children already ran and their state is stable.
        """
        try:
            with self.with_env() as env:
                Task = env['generic.task.queue.task']
                orphans = Task.search([
                    ('worker_id', '=', worker_record_id),
                    ('state', 'in', (
                        'assigned', 'running', 'stuck', 'waiting')),
                ])
                if not orphans:
                    return
                _logger.warning(
                    "Worker startup: found %d orphaned task(s) "
                    "from previous run, applying retry policy",
                    len(orphans))
                for task in orphans:
                    _logger.warning(
                        "  Orphaned task %d (state=%s, "
                        "retry_policy=%s)",
                        task.id, task.state, task.retry_policy)
                    if task.state == 'waiting':
                        # Don't re-execute — just un-own so any worker
                        # can re-check whether children are done.
                        task.write({'worker_id': False})
                    elif task.retry_policy == 'retriable':
                        task.write({
                            'state': 'pending',
                            'worker_id': False,
                            'runner_id': False,
                            'task_error': False,
                            'progress': 0,
                        })
                    else:
                        task.write({
                            'state': 'failed',
                            'task_error': (
                                'Worker restarted during execution '
                                '(previous run was lost)'),
                        })
        except Exception:
            _logger.error(
                "Error cleaning orphaned tasks on startup "
                "for worker_record_id=%d",
                worker_record_id, exc_info=True)

    def _check_waiting_parents(self):
        """ Check waiting parent tasks and complete them
            if all children are done.

            Intentionally does NOT filter by worker_id: a task can enter
            'waiting' state on one worker and need to be completed after
            that worker restarts or after orphan cleanup clears worker_id.
            Concurrency is handled inside _check_waiting_parent() via
            SELECT FOR UPDATE SKIP LOCKED.

            Each task is processed in its own transaction so that a
            failure (or concurrent-update skip) for one task does not
            abort the check for the remaining waiting tasks.
        """
        # Step 1: collect IDs in a short read-only transaction.
        waiting_ids = []
        try:
            with self.with_env() as env:
                waiting_ids = env['generic.task.queue.task'].search([
                    ('state', '=', 'waiting'),
                ], limit=10).ids
        except Exception:
            _logger.error(
                "Error searching for waiting parents", exc_info=True)
            return

        # Step 2: process each task in its own transaction.
        for task_id in waiting_ids:
            try:
                with self.with_env() as env:
                    env['generic.task.queue.task'].browse(
                        task_id)._check_waiting_parent()
            except psycopg2.errors.SerializationFailure:
                # SELECT FOR UPDATE SKIP LOCKED raises SerializationFailure
                # when another transaction is mid-UPDATE on the same row
                # (distinct from the normal "row locked → skip" path).
                # This is benign: the concurrent transaction is completing
                # the parent; we will pick it up on the next poll cycle
                # if still needed.
                _logger.debug(
                    "Skipped waiting parent check for task %d "
                    "(concurrent update in progress, will retry)",
                    task_id)
            except Exception:
                _logger.error(
                    "Error checking waiting parent %d",
                    task_id, exc_info=True)

    def _notify_parent_on_child_done(self, child_task_id):
        """ Notify parent task that a child has completed.

            Opens its own transaction so it runs with no row locks
            held from the child's execution phase (deadlock-safe).
            Calls on_child_done hook on the parent's task type.
        """
        try:
            with self.with_env() as env:
                child_task = env['generic.task.queue.task'].browse(
                    child_task_id)
                if not child_task.parent_id:
                    return
                parent = child_task.parent_id
                if parent.state != 'waiting':
                    return
                registry = TaskTypeRegistry()
                task_type_cls = registry.get_task_type(parent.type_code)
                task_type = task_type_cls()
                task_type.on_child_done(env, parent, child_task)
        except Exception:
            _logger.error(
                "Error in on_child_done for child task %d",
                child_task_id, exc_info=True)

    def _auto_retry_failed(self):
        """ Automatically retry failed retriable tasks.

            Uses FOR UPDATE SKIP LOCKED to prevent multiple
            workers from retrying the same tasks simultaneously.

            Only tasks with retry_count < max_retries are selected,
            so permanently-exhausted tasks never consume query slots
            or acquire locks.

            NOTE: 'stuck' state is intentionally excluded. Stuck tasks
            are retried only after the worker restarts and mark_dead()
            transitions them to 'failed'. This prevents two threads
            executing the same stuck task simultaneously.
        """
        if not self._channels:
            return
        try:
            with self.with_env() as env:
                if self._task_types:
                    env.cr.execute("""
                        SELECT id FROM generic_task_queue_task
                        WHERE state = 'failed'
                          AND retry_policy = 'retriable'
                          AND retry_count < max_retries
                          AND channel IN %s
                          AND type_code IN %s
                        LIMIT 10
                        FOR UPDATE SKIP LOCKED
                    """, (tuple(self._channels),
                          tuple(self._task_types)))
                else:
                    env.cr.execute("""
                        SELECT id FROM generic_task_queue_task
                        WHERE state = 'failed'
                          AND retry_policy = 'retriable'
                          AND retry_count < max_retries
                          AND channel IN %s
                        LIMIT 10
                        FOR UPDATE SKIP LOCKED
                    """, (tuple(self._channels),))
                task_ids = [r[0] for r in env.cr.fetchall()]
                if not task_ids:
                    return
                _logger.debug(
                    "Auto-retrying %d failed task(s): %s",
                    len(task_ids), task_ids)
                registry = TaskTypeRegistry()
                tasks = env['generic.task.queue.task'].browse(task_ids)
                for task in tasks:
                    eta = self._retry_eta(
                        registry, task.type_code,
                        task.retry_count)
                    try:
                        task.action_retry(eta=eta)
                        _logger.info(
                            "Task %d (type=%s, retry_count=%d/%d) "
                            "queued for retry%s",
                            task.id, task.type_code,
                            task.retry_count, task.max_retries,
                            " (eta=%s)" % eta if eta else "")
                    except Exception:
                        _logger.error(
                            "Error retrying task %d",
                            task.id, exc_info=True)
        except Exception:
            _logger.error(
                "Error auto-retrying failed tasks", exc_info=True)

    @staticmethod
    def _retry_eta(registry, type_code, retry_count):
        """ Return the datetime after which the next retry should run,
            or None for immediate execution.

            Reads _retry_delays from the registered task type class:
              - dict  {retry_count: seconds}: explicit per-attempt delay
              - 'exponential': min(2**retry_count, 3600) seconds
              - {} / missing key: None (immediate)
        """
        try:
            cls = registry.get_task_type(type_code)
        except KeyError:
            return None
        delays = cls._retry_delays
        if not delays:
            return None
        if delays == 'exponential':
            delay = min(2 ** retry_count, 3600)
        else:
            delay = delays.get(retry_count, 0)
        if not delay:
            return None
        return datetime.utcnow() + timedelta(seconds=delay)

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
