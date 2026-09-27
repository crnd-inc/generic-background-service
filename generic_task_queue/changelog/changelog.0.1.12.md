**Progress writes never block on a task-row lock**

`update_progress()` skips rows locked by another transaction
(`FOR UPDATE SKIP LOCKED`) and drops the tick; the lock holder is about
to write an authoritative state anyway.
