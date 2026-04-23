from odoo.addons.generic_task_queue import AbstractTaskType


class TestTaskTypeNoOp(AbstractTaskType):
    """Task type that does nothing. For registry tests."""
    _name = 'test.task.type.noop'
    _singleton = False

    def execute(self, env, task):
        return {'status': 'noop'}


class TestTaskTypeEcho(AbstractTaskType):
    """Task type that returns its params back. For testing."""
    _name = 'test.task.type.echo'
    _singleton = False

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
    _singleton = False
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


class TestTaskTypePropagatingChild(AbstractTaskType):
    """Child task type that propagates its progress to the parent.

    Used to test the _propagate_progress flag.
    """
    _name = 'test.task.type.propagating.child'
    _singleton = False
    _propagate_progress = True

    def execute(self, env, task):
        return {}


class TestTaskTypeBatchChild(AbstractTaskType):
    """Child task type that processes a chunk of items."""
    _name = 'test.task.type.batch.child'
    _singleton = False

    def execute(self, env, task):
        items = task.task_params.get('items', [])
        # Simulate processing: double each item
        processed = [i * 2 for i in items]
        return {'processed': processed}


class TestRoutingAnyServiceType(AbstractTaskType):
    """Routing test: claimable by any service (_service_name=None)."""
    _name = 'test.routing.any.service'
    _service_name = None
    _singleton = False
    _default_channel = 'default'

    def execute(self, env, task):
        return {}


class TestRoutingSpecificServiceType(AbstractTaskType):
    """Routing test: locked to 'my.specific.service'."""
    _name = 'test.routing.specific.service'
    _service_name = 'my.specific.service'
    _singleton = False
    _default_channel = 'specific'

    def execute(self, env, task):
        return {}


class TestRoutingOtherServiceType(AbstractTaskType):
    """Routing test: locked to 'other.service'."""
    _name = 'test.routing.other.service'
    _service_name = 'other.service'
    _singleton = False
    _default_channel = 'other'

    def execute(self, env, task):
        return {}


class TestRoutingCustomChannelType(AbstractTaskType):
    """Routing test: custom _default_channel='heavy'."""
    _name = 'test.routing.custom.channel'
    _service_name = None
    _singleton = False
    _default_channel = 'heavy'

    def execute(self, env, task):
        return {}
