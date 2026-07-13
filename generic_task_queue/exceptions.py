import psycopg2.errors

# psycopg2 error types that are always transient and safe to retry. These
# abort the whole PostgreSQL transaction (a savepoint rollback does NOT
# recover them), so callers must roll back the outer transaction and retry
# rather than trying to write (e.g. mark a task failed) afterwards.
KNOWN_TRANSIENT_ERRORS = (
    psycopg2.errors.SerializationFailure,
    psycopg2.errors.DeadlockDetected,
    psycopg2.errors.LockNotAvailable,
)


class RetryTask(Exception):
    """Raised by execute() to request an automatic retry.

    Works with retry_known and retry_any policies.
    Subject to max_retries — the retry budget is still enforced.

    :param after: timedelta to wait before the next attempt, or None
        to use the task type's _retry_delays schedule.

    Example::

        raise RetryTask(after=timedelta(seconds=30))
    """

    def __init__(self, after=None):
        self.after = after
        super().__init__(
            "Task requested retry"
            + (" after %s" % after if after else ""))


class ChildTasksFailedError(Exception):
    """Passed to on_failure() when a waiting parent task is failed because
    one or more of its children failed non-retriably.

    Lets a task type's on_failure() hook distinguish "my own execute() raised"
    from "my children failed" — the parent never executed business logic in
    the latter case. Carries the failed child tasks for inspection.

    :param failed_children: recordset of the children that failed.
    """

    def __init__(self, failed_children):
        self.failed_children = failed_children
        super().__init__(
            "Child tasks failed: %s" % ', '.join(
                failed_children.mapped('name')))


class AlreadyScheduledException(Exception):
    """Raised by create_task() when on_conflict='raise' and a task with
    the same unique_key is already active."""

    def __init__(self, task):
        self.task = task
        super().__init__(
            "Task with unique_key %r is already active "
            "(id=%d, state=%s)" % (task.unique_key, task.id, task.state)
        )
