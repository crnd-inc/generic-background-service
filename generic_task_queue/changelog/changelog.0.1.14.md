**Crash recovery: unified, budgeted, convergent**

Peer-detected dead workers, restart after an unclean stop and a task
thread gone after a timeout all recover through `_handle_lost_execution`:

- Ownership is re-checked under a row lock (`SKIP LOCKED`) on both
  sides: recovery never requeues a task whose own thread is committing
  a result, and the thread's own final write (`action_done`,
  `action_fail`, auto-retry) is dropped once recovery has requeued or
  failed the task, instead of overwriting it.
- `assigned` (claimed, never started) → requeued, retry budget untouched.
- Retriable with budget remaining → requeued, advancing `retry_count`
  and honouring `_retry_delays`. **Lost executions consume the retry
  budget**: a mid-run worker restart (deploys included) counts against
  `max_retries`, so a `retry_any`/`retry_known` task with
  `max_retries = 0` fails instead of requeueing forever. Manual retry
  stays unlimited.
- Otherwise → `on_failure(TaskAbandonedError)` + `action_fail`.

`check_stale_workers()` only marks stale workers dead. The detecting
worker then recovers their tasks in a transaction of its own, at most
`RECOVERY_BATCH_SIZE` per cycle, so hooks never run under the worker-row
locks. The sweep repeats after every stale check, so it converges. The
worker and runner a task had when found are verified under the lock, so
a task re-claimed meanwhile is left to its new runner.

**Graceful shutdown waits 60 s** (was 10 s) for in-flight task threads,
so a task finishing during a restart writes its own result instead of
being charged a lost execution. `TaskQueueService._shutdown_timeout` is
75 s to cover it.
