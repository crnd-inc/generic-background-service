from odoo.addons.generic_task_queue import AbstractTaskType


class TestTaskTypeNoOp(AbstractTaskType):
    """Task type that does nothing. For registry tests."""
    _name = 'test.task.type.noop'

    def execute(self, env, task):
        return {'status': 'noop'}


class TestTaskTypeEcho(AbstractTaskType):
    """Task type that returns its params back. For testing."""
    _name = 'test.task.type.echo'

    def execute(self, env, task):
        return task.task_params
