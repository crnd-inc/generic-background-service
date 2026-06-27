**`on_failure` fires when a child task fails the parent**

When a waiting parent is failed because a child failed non-retriably, the parent
task type's `on_failure(env, task, exc)` hook now runs, with `exc` a
`ChildTasksFailedError` (`exc.failed_children` holds the failed children).
Previously `on_failure` ran only on the parent's own `execute()` error, so task
types that finalize external bookkeeping in `on_failure` missed child-induced
failures.
