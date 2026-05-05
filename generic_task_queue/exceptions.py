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


class AlreadyScheduledException(Exception):
    """Raised by create_task() when on_conflict='raise' and a task with
    the same unique_key is already active."""

    def __init__(self, task):
        self.task = task
        super().__init__(
            "Task with unique_key %r is already active "
            "(id=%d, state=%s)" % (task.unique_key, task.id, task.state)
        )
