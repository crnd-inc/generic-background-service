# Changelog

## Release 18.0.1.4.0 (2026-Oct-26 20:08:54)

### Updated addons

- `test_generic_task_queue` (18.0.0.0.12 -> 18.0.0.1.0)
- `generic_task_queue` (18.0.0.1.10 -> 18.0.0.2.0)
- `generic_background_service` (18.0.0.2.4 -> 18.0.0.3.0)

### Notable changes

#### Addon `generic_task_queue`

##### Version 0.1.14
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

##### Version 0.1.13
**`on_failure` fires when `on_all_children_done` raises**, like every
other path to `failed`, so task types can finalize external bookkeeping
there.

**`TaskAbandonedError`** (exported from the package root): what
`on_failure` receives when a task is failed because its execution was
lost (worker died or restarted, task thread gone).

**Hook delivery is at-least-once**: a starved worker can be declared
dead by a peer while its thread still finishes, so `on_success` /
`on_failure` may fire more than once for the same task. Side effects
must be idempotent.

##### Version 0.1.12
**Progress writes never block on a task-row lock**

`update_progress()` skips rows locked by another transaction
(`FOR UPDATE SKIP LOCKED`) and drops the tick; the lock holder is about
to write an authoritative state anyway.

##### Version 0.1.11
**Fix: `gtq_task_auto_refresh` widget crashed when `label_field` / `state_field` /
`progress_field` options were not set**

`extractProps` fell back to `null` for these options, but the OWL component
declares them as `{ type: String, optional: true }`, which accepts a missing
key or `undefined` but not `null`. Any view using the widget without every
one of these three options (the common case, since all three are optional)
failed to render with `OwlError: Invalid props for component
'TaskAutoRefreshField'`. The fallback is now `undefined`, matching the props
declaration.


#### Addon `generic_background_service`

##### Version 0.3.0
- [IMP] `_service_shutdown_timeout` raised to 90 s so a service that is
  still draining its workers within its own `_shutdown_timeout` is not
  reported as failing to stop.

##### Version 0.2.5
- [ADD] Declared the base module dependency explicitly so frontend test suites
  can resolve the complete addon dependency graph.


