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


class TestTaskTypeBatchParent(AbstractTaskType):
    """Task type that splits work into child tasks.

    Demonstrates parent/child pattern:
    - Parent receives list of items in params
    - Creates one child per chunk
    - Waits for all children to complete
    - Aggregates results
    """
    _name = 'test.task.type.batch.parent'
    _track_progress = True

    _chunk_size = 2

    def execute(self, env, task):
        items = task.task_params.get('items', [])
        Task = env['generic.task.queue.task']

        # Split items into chunks and create child tasks
        chunks = [
            items[i:i + self._chunk_size]
            for i in range(0, len(items), self._chunk_size)
        ]
        Task.create_children(task, 'test.task.type.batch.child', [
            {'items': chunk} for chunk in chunks
        ])

        # Transition to waiting
        task.action_wait_children()

    def on_all_children_done(self, env, parent_task):
        all_results = []
        for cr in self.iter_child_results(parent_task):
            if cr.result:
                all_results.extend(cr.result.get('processed', []))
        return {
            'total_processed': len(all_results),
            'items': all_results,
        }


class TestTaskTypeBatchChild(AbstractTaskType):
    """Child task type that processes a chunk of items."""
    _name = 'test.task.type.batch.child'

    def execute(self, env, task):
        items = task.task_params.get('items', [])
        # Simulate processing: double each item
        processed = [i * 2 for i in items]
        return {'processed': processed}
