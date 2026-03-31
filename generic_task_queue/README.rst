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
  it auto-registers in the task type registry
- **Built-in ModelMethodTaskType** -- run any Odoo model method as a
  background task without writing a custom task type
- **Atomic task claiming** -- uses ``SELECT ... FOR UPDATE SKIP LOCKED``
  to prevent race conditions between concurrent workers
- **Parallel execution** -- workers can run multiple tasks simultaneously
  (configurable ``_max_parallel_jobs``)
- **Progress tracking** -- task types report progress (0-100) visible in UI
- **Cooperative cancellation** -- cancel running tasks from the UI;
  task types check ``task.is_cancelled()`` and exit early
- **Automatic retry** -- failed retriable tasks are automatically retried
  up to ``max_retries`` times
- **Task timeout** -- workers detect and fail tasks that exceed their timeout
- **Parent/child tasks** -- split large jobs into sub-tasks
- **Worker health monitoring** -- heartbeat-based detection of dead workers
  with automatic task reassignment
- **Channel-based routing** -- route tasks to specific workers via channels


Quick start
===========

**Run a model method in background (simplest):**

.. code-block:: python

    # From any model -- one-liner
    self._g_task_queue__plan('do_heavy_work', amount=5)

    # With options
    self._g_task_queue__plan(
        'sync_to_api',
        channel='heavy',
        timeout=3600,
        priority=1,
    )

**Create a task via the task model:**

.. code-block:: python

    self.env['generic.task.queue.task'].create_task(
        'task.type.model.method',
        name='Process records',
        params={
            'model': 'my.model',
            'method': 'process_batch',
            'record_ids': record.ids,
            'kwargs': {'force': True},
        },
        channel='default',
        priority=5,
    )

**Define a custom task type:**

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


Configuration
=============

Add ``generic_task_queue`` to ``server_wide_modules`` in your Odoo config
for the worker to start automatically::

    server_wide_modules = base,web,generic_background_service,generic_task_queue

Modules that only *create* tasks (not define custom task types)
just need ``generic_task_queue`` in their ``depends`` -- no
``server_wide_modules`` entry required.


Architecture
============

- **Task Type Registry** -- Python-only singleton that auto-discovers
  task type classes via ``__init_subclass__``
- **Task Model** (``generic.task.queue.task``) -- Odoo model storing
  task records with state machine
  (pending -> assigned -> running -> done/failed/cancelled)
- **Worker Model** (``generic.task.queue.worker``) -- tracks active
  workers with heartbeat for health monitoring
- **TaskQueueService** -- ``BackgroundService`` subclass that spawns
  one worker per database
- **TaskQueueWorker** -- thread manager that runs task execution in
  separate threads, handles heartbeat, timeouts, and auto-retry


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
