class AlreadyScheduledException(Exception):
    """Raised by create_task() when on_conflict='raise' and a task with
    the same unique_key is already active."""

    def __init__(self, task):
        self.task = task
        super().__init__(
            "Task with unique_key %r is already active "
            "(id=%d, state=%s)" % (task.unique_key, task.id, task.state)
        )
