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
  (configurable ``_max_parallel_jobs``, overridable in ``odoo.conf``)
- **Singleton execution guard** -- ``_singleton = True`` on a task type
  prevents a second instance from running while one is already executing
- **Deduplication guard** -- pass ``unique_key=`` to ``create_task()`` to
  silently reuse or raise when the same logical task is already active
- **Progress tracking** -- task types report progress (0-100) visible in UI
  with real-time updates via bus notifications
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
- **Real-time UI widgets** -- OWL widgets for live progress display without
  polling
- **Security** -- tasks execute as the creating user, field-level write
  protection, ``@api.private`` on worker-internal methods
- **Automatic vacuum** -- configurable cron job cleans up old terminal tasks


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


Deduplication and singleton execution
=====================================

**unique_key — prevent duplicate tasks**

Pass ``unique_key=`` to ``create_task()`` to guard against enqueueing the
same logical task twice while one is already active (pending, assigned,
running, stuck, or waiting):

.. code-block:: python

    env['generic.task.queue.task'].create_task(
        'my.module.sync.products',
        unique_key='rozetka-sync-%d' % product.id,
        # on_conflict='reuse-running'  ← default: return the existing task
        # on_conflict='raise'          ← raise AlreadyScheduledException
    )

- ``on_conflict='reuse-running'`` (default) — returns the existing active
  task without creating a new one.
- ``on_conflict='raise'`` — raises ``AlreadyScheduledException``; the
  exception carries the conflicting task in ``.task``.

Once the task reaches a terminal state (done, failed, cancelled) the key
is released and the same key can be used again.

**_singleton — prevent parallel execution**

Set ``_singleton = True`` on a task type to ensure at most one task of that
type is in the ``assigned`` or ``running`` state cluster-wide. The worker
skips claiming a new task of that type while another is already executing.

``_singleton = True`` is the **default** for all custom task types — safe
for tasks that must not overlap (bulk data fetch, report generation, etc.).

To allow parallel execution, explicitly opt out:

.. code-block:: python

    class ProcessChunkTaskType(AbstractTaskType):
        _name = 'my.module.process.chunk'
        _singleton = False  # chunks are independent, run them in parallel

``task.type.model.method`` (the built-in generic task type) also has
``_singleton = False`` — parallel execution is safe because deduplication
is controlled per-enqueue via ``unique_key``.


Real-time UI widgets
====================

The module ships two OWL widgets that update live via bus notifications
without polling or page refresh.

``gtq_task_progress``
---------------------

Live progress bar for the task model's own list and form views.
Place on the ``progress`` integer field:

.. code-block:: xml

    <field name="progress" widget="gtq_task_progress"/>

Shows a spinner and animated progress bar while the task is active.
Shows the final DB value (e.g. 100%) when the task is done.
Calls ``record.load()`` automatically when the task reaches a terminal
state so the rest of the form refreshes.

``gtq_task_auto_refresh``
-------------------------

For consumer records that reference a task via a Many2one field.
Subscribes to bus notifications for that task and triggers a
``record.load()`` on the consumer record when the task completes.

.. code-block:: xml

    <field name="task_id" widget="gtq_task_auto_refresh"
           options="{
               'state_field': 'task_state',
               'progress_field': 'task_progress',
               'label': 'Processing, please wait...'
           }"/>

Options:

+-------------------+-------------------------------------------------------+
| Option            | Description                                           |
+===================+=======================================================+
| ``show_progress`` | Show spinner + progress bar while active.             |
|                   | Default: ``true``.                                    |
+-------------------+-------------------------------------------------------+
| ``label``         | Text shown above the progress block while active.     |
|                   | Defaults to task display name. Pass ``""`` to hide.   |
+-------------------+-------------------------------------------------------+
| ``state_field``   | Field on the consumer record holding the task state.  |
|                   | Initialises the spinner correctly when the form opens |
|                   | mid-run (cold-start fix). Example: ``'task_state'``.  |
+-------------------+-------------------------------------------------------+
| ``progress_field``| Field on the consumer record holding task progress.   |
|                   | Initialises the bar position on cold open.            |
|                   | Example: ``'task_progress'``.                         |
+-------------------+-------------------------------------------------------+

The widget renders nothing when the task is inactive. It is safe to leave
it in the view at all times.

Sending progress from a task type
----------------------------------

Call ``task.update_progress(value)`` with a value between 0 and 100.
This commits immediately via a separate cursor (visible to the UI without
waiting for the task transaction to finish) and sends a bus notification:

.. code-block:: python

    def execute(self, env, task):
        for i, item in enumerate(task.task_params['items']):
            process(item)
            task.update_progress(int((i + 1) / len(items) * 100))


Configuration
=============

Add ``generic_task_queue`` to ``server_wide_modules`` in your Odoo config
for the worker to start automatically::

    server_wide_modules = base,web,generic_background_service,generic_task_queue

Modules that define ``@background_task`` methods or custom task types
just need ``generic_task_queue`` in their ``depends`` -- no
``server_wide_modules`` entry required. Task types are auto-discovered
and registered in each database where the module is installed.

**Tuning parallel jobs via odoo.conf:**

``_max_parallel_jobs`` can be overridden per deployment without changing
code by adding a ``[generic_task_queue]`` section to ``odoo.conf``.
The key is ``{service_name}_max_parallel_jobs`` with dots replaced by
underscores:

.. code-block:: ini

    [generic_task_queue]
    # Default service
    generic_task_queue_service_max_parallel_jobs = 4
    # Custom service
    my_heavy_task_service_max_parallel_jobs = 2

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


Vacuum / cleanup
================

A daily cron job deletes old terminal tasks (``done``, ``failed``,
``cancelled``). It is installed with ``noupdate="1"`` so you can freely
adjust its schedule from *Technical → Automation → Scheduled Actions*.

Two `System Parameters <https://www.odoo.com/documentation/18.0/developer/reference/backend/system_parameters.html>`_
control the behaviour:

+-------------------------------------------+----------+-----------------+
| Key                                       | Default  | Description     |
+===========================================+==========+=================+
| ``generic_task_queue.vacuum_days``        | ``30``   | Delete tasks    |
|                                           |          | completed more  |
|                                           |          | than N days ago.|
|                                           |          | Set to ``0`` to |
|                                           |          | disable.        |
+-------------------------------------------+----------+-----------------+
| ``generic_task_queue.vacuum_batch_size``  | ``1000`` | Max tasks per   |
|                                           |          | cron run. Tune  |
|                                           |          | upward for high-|
|                                           |          | volume installs.|
+-------------------------------------------+----------+-----------------+

Only root tasks are searched; child tasks are removed automatically via
cascade delete.


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
- **Bus notifications** -- ``gtq_task_update`` and ``gtq_task_progress``
  channels deliver real-time state and progress to the UI

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
==========
.. image:: https://crnd.pro/web/image/3699/300x140/crnd.png

Our web site: https://crnd.pro/

This module is maintained by the Center of Research & Development company.

We can provide you further Odoo Support, Odoo implementation, Odoo customization, Odoo 3rd Party development and integration software, consulting services. Our main goal is to provide the best quality product for you.

For any questions `contact us <mailto:info@crnd.pro>`__.
