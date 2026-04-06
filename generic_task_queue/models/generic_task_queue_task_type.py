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
    default_timeout = fields.Integer(
        default=0,
        help="Default execution timeout in seconds for tasks of this type. "
             "0 means no timeout. Can be overridden by the task-level "
             "timeout field."
    )

    _sql_constraints = [
        ('code_uniq', 'UNIQUE (code)',
         'Task type code must be unique.'),
    ]

    def _register_hook(self):
        """ Sync Python-discovered task types to DB after
            every module install/upgrade. Runs per-database.
        """
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
            )
        self.invalidate_model()

    @api.model
    def _sync_type(self, code, module, name=None, notify_on_completion=False):
        """ Create or update a task type record.

            Uses INSERT ... ON CONFLICT DO UPDATE so concurrent calls
            from multiple worker processes never conflict.  name is only
            set on INSERT; subsequent syncs leave it alone so user
            customisations are preserved.
        """
        self.env.cr.execute("""
            INSERT INTO generic_task_queue_task_type
                (code, module, name, active, notify_on_completion,
                 create_uid, write_uid, create_date, write_date)
            VALUES
                (%(code)s, %(module)s, %(name)s, true,
                 %(notify_on_completion)s, %(uid)s, %(uid)s,
                 (NOW() AT TIME ZONE 'UTC'), (NOW() AT TIME ZONE 'UTC'))
            ON CONFLICT (code) DO UPDATE SET
                module               = EXCLUDED.module,
                active               = true,
                notify_on_completion = EXCLUDED.notify_on_completion,
                write_uid            = EXCLUDED.write_uid,
                write_date           = EXCLUDED.write_date
        """, {
            'code': code,
            'module': module,
            'name': name or code,
            'notify_on_completion': notify_on_completion,
            'uid': self.env.uid,
        })
