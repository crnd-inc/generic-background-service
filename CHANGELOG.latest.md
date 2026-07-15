# Changelog

## Release 18.0.1.2.0 (2026-Jul-15 13:30:58)

### Updated addons

- `test_generic_task_queue` (18.0.0.0.9 -> 18.0.0.0.11)
- `generic_task_queue` (18.0.0.1.7 -> 18.0.0.1.9)

### Notable changes

#### Addon `generic_task_queue`

##### Version 0.1.8
**`on_failure` fires when a child task fails the parent**

When a waiting parent is failed because a child failed non-retriably, the parent
task type's `on_failure(env, task, exc)` hook now runs, with `exc` a
`ChildTasksFailedError` (`exc.failed_children` holds the failed children).
Previously `on_failure` ran only on the parent's own `execute()` error, so task
types that finalize external bookkeeping in `on_failure` missed child-induced
failures.

**Child tasks inherit their task type's retry policy**

`create_children` / `spawn_children` now resolve each child's `retry_policy` and
`max_retries` from its task type's class defaults when the spec does not set
them (parity with `create_task` / `create_tasks`). Previously children fell back
to the field defaults (`no_retry` / `max_retries = 0`), silently ignoring the
retry policy declared on their type — so retries were effectively disabled for
spawned child waves. A deprecated retry-policy alias used as a type default is
normalized to its canonical name, as elsewhere.


