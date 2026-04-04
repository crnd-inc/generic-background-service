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
            self._sync_type(code, module)

    @api.model
    def _sync_type(self, code, module, name=None):
        """ Create or update a task type record. """
        existing = self.search([('code', '=', code)], limit=1)
        if existing:
            vals = {'module': module, 'active': True}
            if name:
                vals['name'] = name
            existing.write(vals)
        else:
            self.create({
                'code': code,
                'module': module,
                'name': name or code,
                'active': True,
            })
