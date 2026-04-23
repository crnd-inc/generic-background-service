import logging

from odoo import models, fields, api

from ..service.task_type_registry import TaskTypeRegistry

_logger = logging.getLogger(__name__)


class GenericTaskQueueTaskType(models.Model):
    _name = 'generic.task.queue.task.type'
    _description = 'Task Queue Task Type'
    _order = 'name'

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    module = fields.Char(
        index=True,
        help="Technical name of the Odoo module that provides "
             "this task type.")
    active = fields.Boolean(default=True)
    description = fields.Text()
    notify_on_completion = fields.Boolean(
        default=False,
        help="If enabled, the task creator receives a toast notification "
             "when a task of this type completes or fails.")
    propagate_progress = fields.Boolean(
        default=False,
        help="If enabled, calling update_progress() on a task of this type "
             "automatically averages sibling progress and writes it to the "
             "parent task, then recurses upward. Use on intermediate task "
             "types in multi-level hierarchies to propagate fine-grained "
             "progress to the root without manual on_child_done hooks.")
    default_timeout = fields.Integer(
        default=0,
        help="Default execution timeout in seconds for tasks of this type. "
             "0 means no timeout. Can be overridden by the task-level "
             "timeout field."
    )
    default_retry_policy = fields.Selection(
        [('retriable', 'Retriable'), ('non_retriable', 'Non-Retriable')],
        readonly=True,
        help="Default retry policy defined in code by the task type. "
             "Override per-task at enqueue time via create_task().")
    default_max_retries = fields.Integer(
        readonly=True,
        help="Default maximum automatic retries defined in code by the "
             "task type. Override per-task at enqueue time via create_task()."
    )
    service_name = fields.Char(
        readonly=True,
        help="Service that exclusively handles this task type. "
             "Empty means any service may claim it.")
    default_channel = fields.Char(
        readonly=True,
        default='default',
        help="Default channel used when enqueueing tasks of this type "
             "without an explicit channel argument.")

    _sql_constraints = [
        ('code_uniq', 'UNIQUE (code)',
         'Task type code must be unique.'),
    ]

    def _register_hook(self):
        """ Sync Python-discovered task types to DB, but only when
            at least one module was installed or upgraded in this startup.

            On a plain restart (no module changes) pool.updated_modules is
            empty, so the sync is skipped — the DB records were already
            written during the last install/upgrade and are still valid.
            This also avoids SerializationFailure from concurrent worker
            processes all trying to upsert the same rows on every restart.
        """
        if self.pool.updated_modules:
            self._sync_from_python_registry()

    @api.model
    def _sync_from_python_registry(self):
        """ Scan TaskTypeRegistry and create/update DB records
            for all discovered task types.
        """
        registered = TaskTypeRegistry._registered_types
        for code, defs in registered.items():
            cls = defs[0]
            # Use Odoo's standard convention: odoo.addons.MODULE.xxx
            parts = cls.__module__.split('.')
            module = parts[2] if len(parts) > 2 else ''
            self._sync_type(
                code, module,
                notify_on_completion=cls._notify_on_completion,
                propagate_progress=cls._propagate_progress,
                default_retry_policy=cls._retry_policy,
                default_max_retries=cls._max_retries,
                service_name=cls._service_name or '',
                default_channel=cls._default_channel or 'default',
            )
        self.invalidate_model()

    @api.model
    def _sync_type(self, code, module, name=None, notify_on_completion=False,
                   propagate_progress=False,
                   default_retry_policy='non_retriable',
                   default_max_retries=0,
                   service_name='',
                   default_channel='default'):
        """ Create or update a task type record.

            Uses INSERT ... ON CONFLICT DO UPDATE so concurrent calls
            from multiple worker processes never conflict.  name is only
            set on INSERT; subsequent syncs leave it alone so user
            customisations are preserved.
        """
        self.env.cr.execute("""
            INSERT INTO generic_task_queue_task_type
                (code, module, name, active, notify_on_completion,
                 propagate_progress,
                 default_retry_policy, default_max_retries,
                 service_name, default_channel,
                 create_uid, write_uid, create_date, write_date)
            VALUES
                (%(code)s, %(module)s, %(name)s, true,
                 %(notify_on_completion)s,
                 %(propagate_progress)s,
                 %(default_retry_policy)s, %(default_max_retries)s,
                 %(service_name)s, %(default_channel)s,
                 %(uid)s, %(uid)s,
                 (NOW() AT TIME ZONE 'UTC'), (NOW() AT TIME ZONE 'UTC'))
            ON CONFLICT (code) DO UPDATE SET
                module                = EXCLUDED.module,
                active                = true,
                notify_on_completion  = EXCLUDED.notify_on_completion,
                propagate_progress    = EXCLUDED.propagate_progress,
                default_retry_policy  = EXCLUDED.default_retry_policy,
                default_max_retries   = EXCLUDED.default_max_retries,
                service_name          = EXCLUDED.service_name,
                default_channel       = EXCLUDED.default_channel,
                write_uid             = EXCLUDED.write_uid,
                write_date            = EXCLUDED.write_date
        """, {
            'code': code,
            'module': module,
            'name': name or code,
            'notify_on_completion': notify_on_completion,
            'propagate_progress': propagate_progress,
            'default_retry_policy': default_retry_policy,
            'default_max_retries': default_max_retries,
            'service_name': service_name,
            'default_channel': default_channel,
            'uid': self.env.uid,
        })
