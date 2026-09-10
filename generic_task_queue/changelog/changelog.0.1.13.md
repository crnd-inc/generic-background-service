**`on_failure` fires when `on_all_children_done` raises**

Failing a waiting parent because its round-boundary hook raised now runs
the task type's `on_failure` first, like the execute-path and
child-failure paths — task types finalize external bookkeeping there
(e.g. marking an owning record failed), and a path to `failed` that
skips the hook leaves that record stranded.

**New `TaskAbandonedError`** (exported from the package root): the
exception `on_failure` receives when a task is failed because its
execution was lost (worker died or restarted, task thread gone), so a
task type can tell a lost execution from a business error. Crash
recovery starts passing it in an upcoming change.

**`assigned` → `failed` is a permitted transition** — a task claimed by
a worker that died before `action_start()` must be failable.

**Hook delivery documented as at-least-once**: a starved worker can be
declared dead by a peer while its thread still finishes, so
`on_success` / `on_failure` may fire more than once for the same task —
side effects must be idempotent.
