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
