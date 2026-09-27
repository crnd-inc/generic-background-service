**Crash recovery: unified, budgeted, convergent**

Peer-detected dead workers, restart after an unclean stop and a task
thread gone after a timeout all recover through `_handle_lost_execution`:

- Ownership is re-checked under a row lock (`SKIP LOCKED`), so recovery
  never requeues a task whose own thread is committing a result.
- `assigned` (claimed, never started) → requeued, retry budget untouched.
- Retriable with budget remaining → requeued, advancing `retry_count`
  and honouring `_retry_delays`. **Lost executions consume the retry
  budget**: a mid-run worker restart (deploys included) counts against
  `max_retries`, so a `retry_any`/`retry_known` task with
  `max_retries = 0` fails instead of requeueing forever. Manual retry
  stays unlimited.
- Otherwise → `on_failure(TaskAbandonedError)` + `action_fail`.

`check_stale_workers()` also sweeps in-flight tasks of already-dead
workers every cycle, so a task skipped by `mark_dead()` (row locked,
transient error) is recovered later instead of sitting behind its dead
worker forever.

**Graceful shutdown waits 60 s** (was 10 s) for in-flight task threads,
so a task finishing during a restart writes its own result instead of
being charged a lost execution. `TaskQueueService._shutdown_timeout` is
75 s to cover it.
