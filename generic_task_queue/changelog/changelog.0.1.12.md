**Progress writes never block on a task-row lock**

Progress is now explicitly best-effort: locked rows are skipped
(`FOR UPDATE SKIP LOCKED`) and the tick is dropped — the lock holder is
about to write an authoritative state anyway.
