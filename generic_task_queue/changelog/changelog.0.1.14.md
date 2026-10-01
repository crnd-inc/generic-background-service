**Crash recovery: unified, budgeted, convergent**

Dead peers, unclean restarts and task threads gone after a timeout all
recover through `_handle_lost_execution`, under a row lock that verifies
the task's state, runner and worker:

- A task thread's final write (`action_done`, `action_fail`, auto-retry)
  takes the same lock, so neither side overwrites the other.
- `assigned` (never started) → requeued, retry budget untouched.
- Retriable with budget remaining → requeued, advancing `retry_count`
  and honouring `_retry_delays`. **Lost executions consume the retry
  budget**: a mid-run restart counts against `max_retries`, so a
  `retry_any`/`retry_known` task with `max_retries = 0` now fails
  instead of requeueing forever. Manual retry stays unlimited.
- Otherwise → `on_failure(TaskAbandonedError)` + `action_fail`.

`check_stale_workers()` only marks stale workers dead; the detecting
worker recovers their tasks in a separate transaction, at most
`RECOVERY_BATCH_SIZE` per cycle, repeated every stale check.

**Graceful shutdown waits 60 s** (was 10 s) for in-flight task threads,
so a task finishing during a restart writes its own result instead of
being charged a lost execution. `TaskQueueService._shutdown_timeout` is
75 s to cover it.
