Generic Task Queue
==================

.. |badge2| image:: https://img.shields.io/badge/license-LGPL--3-blue.png
    :target: http://www.gnu.org/licenses/lgpl-3.0-standalone.html
    :alt: License: LGPL-3

.. |badge5| image:: https://img.shields.io/badge/maintainer-CR&D-purple.png
    :target: https://crnd.pro/


|badge2| |badge5|


Declarative background task queue for Odoo, built on top of
`Generic Background Service <https://github.com/crnd-inc/generic-background-service>`_.

Tasks are Odoo records with JSON parameters. Task types are Python classes
that define how to execute each category of work.
Workers poll the queue, claim tasks, and execute them in separate threads.

Key features:

- **Declarative task types** -- define a Python class with ``execute()``,
  it auto-registers in the task type registry and syncs to the DB
- **Built-in ModelMethodTaskType** -- run any ``@background_task``-decorated
  model method as a background task without writing a custom task type
- **Atomic task claiming** -- uses ``SELECT ... FOR UPDATE SKIP LOCKED``
  to prevent race conditions between concurrent workers
- **Parallel execution** -- workers can run multiple tasks simultaneously
  (configurable ``_max_parallel_jobs``)
- **Progress tracking** -- task types report progress (0-100) visible in UI
- **Cooperative cancellation** -- cancel running tasks from the UI;
  task types check ``task.is_cancelled()`` and exit early
- **Automatic retry** -- failed retriable tasks are automatically retried
  up to ``max_retries`` times; manual retry always allowed from UI
- **Task timeout** -- workers detect and fail tasks that exceed their timeout
- **Parent/child tasks** -- split large jobs into sub-tasks with automatic
  result aggregation (``on_all_children_done`` hook)
- **Worker health monitoring** -- heartbeat-based detection of dead workers
  with automatic task reassignment
- **Channel-based routing** -- route tasks to specific workers via channels
- **Security** -- tasks execute as the creating user, field-level write
  protection, ``@api.private`` on worker-internal methods


Quick start
===========

**Option 1: Run a model method in background (simplest, no custom task type needed)**

Decorate your method with ``@background_task``:

.. code-block:: python

    from odoo.addons.generic_task_queue.tools.decorators import (
        background_task,
    )

    class MyModel(models.Model):
        _name = 'my.model'

        @background_task
        def do_heavy_work(self, amount=1):
            for record in self:
                record.value += amount

Then plan a task from anywhere:

.. code-block:: python

    # From a recordset -- uses self.ids by default
    self._g_task_queue__plan('do_heavy_work', amount=5)

    # With options
    self._g_task_queue__plan(
        'sync_to_api',
        channel='heavy',
        timeout=3600,
        priority=1,
    )

**Option 2: Create a task via the task model**

.. code-block:: python

    self.env['generic.task.queue.task'].create_task(
        'task.type.model.method',
        name='Process records',
        params={
            'model': 'my.model',
            'method': 'do_heavy_work',
            'record_ids': record.ids,
            'kwargs': {'amount': 5},
        },
    )

**Option 3: Define a custom task type (for complex logic)**

.. code-block:: python

    from odoo.addons.generic_task_queue import AbstractTaskType

    class ConvertVideoTaskType(AbstractTaskType):
        _name = 'my.module.convert.video'

        def execute(self, env, task):
            record = env['my.model'].browse(
                task.task_params['record_id'])

            task.update_progress(10)
            record.download_source()

            if task.is_cancelled():
                return {'status': 'cancelled'}

            task.update_progress(50)
            record.run_conversion()

            task.update_progress(90)
            record.upload_result()

            return {'status': 'done'}

Custom task types auto-register with the default worker -- no need to
create a separate service. Just define the class and create tasks.

**Option 4: Parent/child tasks for batch processing**

.. code-block:: python

    class BatchProcessTaskType(AbstractTaskType):
        _name = 'my.module.batch.process'

        def execute(self, env, task):
            items = task.task_params['items']
            # Split into chunks and create child tasks
            Task = env['generic.task.queue.task']
            chunks = [items[i:i+100] for i in range(0, len(items), 100)]
            Task.create_children(task, 'my.module.process.chunk', [
                {'items': chunk} for chunk in chunks
            ])
            # Parent waits for all children to complete
            task.action_wait_children()

        def on_all_children_done(self, env, parent_task):
            # Aggregate results from all children
            results = []
            for child in parent_task.child_ids:
                results.extend(child.task_result.get('ids', []))
            return {'total': len(results), 'ids': results}


Configuration
=============

Add ``generic_task_queue`` to ``server_wide_modules`` in your Odoo config
for the worker to start automatically::

    server_wide_modules = base,web,generic_background_service,generic_task_queue

Modules that define ``@background_task`` methods or custom task types
just need ``generic_task_queue`` in their ``depends`` -- no
``server_wide_modules`` entry required. Task types are auto-discovered
and registered in each database where the module is installed.

**When you need a separate service (rare):**

A separate service is only needed when you want different operational
characteristics: different channels, different parallelism, or
running on a separate machine/container.

.. code-block:: python

    from odoo.addons.generic_task_queue.service.task_queue_service import (
        TaskQueueService,
    )

    class HeavyTaskService(TaskQueueService):
        _name = 'my.heavy.task.service'
        _require_module = 'my_module'
        _channels = ['heavy']
        _max_parallel_jobs = 4

For most use cases, the default worker handles everything.


Architecture
============

- **Task Type** (``generic.task.queue.task.type``) -- Odoo model storing
  available task types per database, auto-synced from Python registry
  via ``_register_hook()``
- **Task Type Registry** -- Python singleton that auto-discovers
  task type classes via ``__init_subclass__``; DB model is the
  per-database source of truth
- **Task** (``generic.task.queue.task``) -- Odoo model storing
  task records with state machine
  (pending -> assigned -> running -> waiting -> done/failed/cancelled)
- **Worker** (``generic.task.queue.worker``) -- tracks active
  workers with heartbeat for health monitoring
- **TaskQueueService** -- ``BackgroundService`` subclass that spawns
  one worker per database; default service handles all task types
- **TaskQueueWorker** -- thread manager that runs task execution in
  separate threads, handles heartbeat, timeouts, and auto-retry

Security:

- Tasks execute as the creating user (not SUPERUSER)
- Only ``@background_task``-decorated methods can be called via
  ``ModelMethodTaskType``
- Field-level write protection: only whitelisted fields are writable
  by regular users; state transitions use ``sudo()`` internally
- Worker-internal methods are ``@api.private`` (not callable via RPC)
- Record rules: users see only their own tasks


Bug Tracker
===========

Bugs are tracked on `GitHub Issues <https://github.com/crnd-inc/generic-background-service/issues>`_.
In case of trouble, please check there if your issue has already been reported.


Maintainer
''''''''''
.. image:: https://crnd.pro/web/image/3699/300x140/crnd.png

Our web site: https://crnd.pro/

This module is maintained by the Center of Research & Development company.

We can provide you further Odoo Support, Odoo implementation, Odoo customization, Odoo 3rd Party development and integration software, consulting services. Our main goal is to provide the best quality product for you.

For any questions `contact us <mailto:info@crnd.pro>`__.
