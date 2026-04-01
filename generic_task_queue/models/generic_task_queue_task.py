import logging

from odoo import models, fields, api, exceptions

_logger = logging.getLogger(__name__)

TASK_STATES = [
    ('pending', 'Pending'),
    ('assigned', 'Assigned'),
    ('running', 'Running'),
    ('done', 'Done'),
    ('failed', 'Failed'),
    ('cancelled', 'Cancelled'),
]

# Allowed state transitions: {from_state: [to_states]}
ALLOWED_TRANSITIONS = {
    'pending': ['assigned', 'cancelled'],
    'assigned': ['running', 'cancelled'],
    'running': ['done', 'failed', 'cancelled'],
    'failed': ['pending', 'cancelled'],
    'done': [],
    'cancelled': [],
}

RETRY_POLICIES = [
    ('retriable', 'Retriable'),
    ('non_retriable', 'Non-retriable'),
]


class GenericTaskQueueTask(models.Model):
    _name = 'generic.task.queue.task'
    _description = 'Task Queue Task'
    _order = 'priority, date_created'

    def init(self):
        """ Create partial composite index for claim_task query. """
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS
                generic_task_queue_task_claim_idx
            ON generic_task_queue_task
                (priority, date_created)
            WHERE state = 'pending'
        """)

    name = fields.Char(required=True)
    type_code = fields.Char(
        required=True, index=True,
        help="Dotted name of the task type "
             "(e.g. 'task.type.model.method')")
    state = fields.Selection(
        TASK_STATES, default='pending',
        required=True, index=True, readonly=True)
    channel = fields.Char(
        default='default', required=True, index=True,
        help="Routing channel. Workers only pick up tasks "
             "matching their channels.")
    priority = fields.Integer(
        default=5,
        help="Lower number = higher priority (0 is highest).")
    task_params = fields.Json(
        string='Task Parameters', default=dict,
        help="JSON input for the task type's execute() method.")
    task_result = fields.Json(
        readonly=True,
        help="JSON output from the task type's execute() method.")
    task_error = fields.Text(
        readonly=True,
        help="Error traceback if the task failed.")
    worker_id = fields.Many2one(
        'generic.task.queue.worker', readonly=True,
        index=True, ondelete='set null')
    eta = fields.Datetime(
        string='ETA', index=True,
        help="Earliest time this task should be executed. "
             "Leave empty for immediate execution.")
    retry_policy = fields.Selection(
        RETRY_POLICIES, default='retriable', required=True)
    max_retries = fields.Integer(default=3)
    retry_count = fields.Integer(default=0, readonly=True)
    timeout = fields.Integer(
        default=0,
        help="Maximum execution time in seconds. "
             "0 means no timeout. Worker will mark the task "
             "as failed if execution exceeds this limit.")
    progress = fields.Integer(
        default=0, readonly=True,
        help="Execution progress (0-100).")
    parent_id = fields.Many2one(
        'generic.task.queue.task', index=True, ondelete='cascade',
        help="Parent task. Used for splitting work into sub-tasks.")
    child_ids = fields.One2many(
        'generic.task.queue.task', 'parent_id',
        string='Sub-tasks')
    date_created = fields.Datetime(
        default=fields.Datetime.now, required=True,
        index=True, readonly=True)
    date_started = fields.Datetime(readonly=True)
    date_completed = fields.Datetime(readonly=True)

    # Fields that regular users are allowed to modify.
    # All other fields require sudo (system) access.
    # New fields are protected by default — safe default.
    _user_writable_fields = frozenset({
        'name', 'priority', 'channel', 'eta',
        'timeout', 'max_retries', 'retry_policy',
        'task_params',
    })

    @api.model_create_multi
    def create(self, vals_list):
        """ Validate type_code on creation. """
        from ..service.task_type_registry import TaskTypeRegistry
        registry = TaskTypeRegistry()
        available = registry.get_initialized_types()
        for vals in vals_list:
            type_code = vals.get('type_code')
            if type_code and type_code not in available:
                raise exceptions.ValidationError(
                    self.env._(
                        "Unknown task type '%(type_code)s'. "
                        "Available types: %(available)s",
                        type_code=type_code,
                        available=', '.join(sorted(available.keys())),
                    ))
        return super().create(vals_list)

    def write(self, vals):
        """ Protect fields not in _user_writable_fields from
            non-system users. """
        if not self.env.su:
            forbidden = set(vals.keys()) - self._user_writable_fields
            if forbidden:
                raise exceptions.AccessError(
                    self.env._(
                        "You cannot modify fields: %(fields)s. "
                        "Use the task action buttons instead.",
                        fields=', '.join(sorted(forbidden)),
                    ))
        return super().write(vals)

    def _check_transition(self, new_state):
        """ Validate that the state transition is allowed.
        """
        for record in self:
            allowed = ALLOWED_TRANSITIONS.get(record.state, [])
            if new_state not in allowed:
                raise exceptions.ValidationError(
                    self.env._(
                        "Cannot transition task '%(name)s' "
                        "from '%(old_state)s' to '%(new_state)s'.",
                        name=record.name,
                        old_state=record.state,
                        new_state=new_state,
                    ))

    @api.private
    def action_assign(self, worker):
        """ Transition: pending → assigned.

            Called by worker (SUPERUSER context). No sudo needed.
        """
        self._check_transition('assigned')
        self.write({
            'state': 'assigned',
            'worker_id': worker.id,
        })

    @api.private
    def action_start(self):
        """ Transition: assigned → running.

            Called by worker (SUPERUSER context). No sudo needed.
        """
        self._check_transition('running')
        self.write({
            'state': 'running',
            'date_started': fields.Datetime.now(),
            'progress': 0,
        })

    @api.private
    def action_done(self, result=None):
        """ Transition: running → done.

            Called by worker (SUPERUSER context). No sudo needed.
        """
        self._check_transition('done')
        self.write({
            'state': 'done',
            'task_result': result,
            'date_completed': fields.Datetime.now(),
            'progress': 100,
        })

    @api.private
    def action_fail(self, error=None):
        """ Transition: running → failed.

            Called by worker (SUPERUSER context). No sudo needed.
        """
        self.ensure_one()
        self._check_transition('failed')
        self.write({
            'state': 'failed',
            'task_error': error,
            'date_completed': fields.Datetime.now(),
            'retry_count': self.retry_count + 1,
        })

    def action_retry(self):
        """ Transition: failed → pending (if retriable).

            Callable by task owner from UI. Uses sudo() to write
            protected fields.
        """
        for record in self:
            if record.state != 'failed':
                raise exceptions.ValidationError(
                    self.env._("Only failed tasks can be retried."))
            if record.retry_policy != 'retriable':
                raise exceptions.ValidationError(
                    self.env._(
                        "Task '%(name)s' is not retriable.",
                        name=record.name))
            if record.retry_count >= record.max_retries:
                raise exceptions.ValidationError(
                    self.env._(
                        "Task '%(name)s' has exceeded max retries "
                        "(%(max_retries)d).",
                        name=record.name,
                        max_retries=record.max_retries))
        self.sudo().write({
            'state': 'pending',
            'worker_id': False,
            'task_error': False,
            'progress': 0,
        })

    def action_cancel(self):
        """ Transition: pending/assigned/running → cancelled.

            Callable by task owner from UI. Uses sudo() to write
            protected fields. Cascades to non-terminal children.
        """
        self._check_transition('cancelled')
        self.sudo().write({
            'state': 'cancelled',
            'date_completed': fields.Datetime.now(),
        })
        # Cancel non-terminal children
        children_to_cancel = self.mapped('child_ids').filtered(
            lambda c: c.state in ('pending', 'assigned', 'running'))
        if children_to_cancel:
            children_to_cancel.action_cancel()

    @api.private
    def update_progress(self, value):
        """ Update progress using direct SQL on a separate cursor.

            This commits immediately so progress is visible to
            other transactions (e.g., UI polling) without waiting
            for the execute() transaction to complete.
        """
        value = max(0, min(100, int(value)))
        # Use a new cursor to avoid interfering with the
        # current transaction
        new_cr = self.pool.cursor()
        try:
            new_cr.execute(
                "UPDATE generic_task_queue_task "
                "SET progress = %s WHERE id IN %s",
                (value, tuple(self.ids)))
            new_cr.commit()
        finally:
            new_cr.close()

    @api.private
    def is_cancelled(self):
        """ Check if cancellation has been requested.

            Task types should call this periodically during
            long-running execute() and return early if True.

            Uses a separate cursor to see cancellation from
            other transactions immediately.
        """
        self.ensure_one()
        new_cr = self.pool.cursor()
        try:
            new_cr.execute(
                "SELECT state FROM generic_task_queue_task "
                "WHERE id = %s",
                (self.id,))
            row = new_cr.fetchone()
            return row and row[0] == 'cancelled'
        finally:
            new_cr.close()

    @api.private
    @api.model
    def claim_task(self, worker, channels, task_types, limit=1):
        """ Atomically claim pending tasks for a worker.

            Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent
            race conditions between concurrent workers.

            :param worker: generic.task.queue.worker record
            :param list channels: channels this worker handles
            :param list task_types: task type codes this worker handles
            :param int limit: max number of tasks to claim
            :return: recordset of claimed tasks
        """
        if not channels or not task_types:
            return self.browse()
        # Flush pending ORM writes so raw SQL sees current state
        self.flush_model()
        self.env.cr.execute("""
            SELECT id FROM generic_task_queue_task
            WHERE state = 'pending'
              AND channel IN %s
              AND type_code IN %s
              AND (eta IS NULL OR eta <= (NOW() AT TIME ZONE 'UTC'))
            ORDER BY priority, date_created
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        """, (tuple(channels), tuple(task_types), limit))
        task_ids = [r[0] for r in self.env.cr.fetchall()]
        if task_ids:
            tasks = self.browse(task_ids)
            tasks.action_assign(worker)
            return tasks
        return self.browse()

    @api.model
    def create_task(self, type_code, name=None, params=None,
                    channel='default', priority=5, eta=None,
                    timeout=0, parent_id=None,
                    retry_policy='retriable', max_retries=3):
        """ Convenience method to create a task.

            Usage::

                self.env['generic.task.queue.task'].create_task(
                    'task.type.model.method',
                    name='Recompute stats',
                    params={
                        'model': 'flight.record',
                        'method': 'recompute_statistics',
                        'record_ids': [1, 2, 3],
                    },
                    channel='heavy',
                    priority=1,
                )

            :param str type_code: task type dotted name
            :param str name: task description (auto-generated if omitted)
            :param dict params: task parameters (JSON-serializable)
            :param str channel: routing channel
            :param int priority: 0 = highest
            :param datetime eta: earliest execution time
            :param int timeout: max execution seconds (0 = no limit)
            :param int parent_id: parent task ID for sub-tasks
            :param str retry_policy: 'retriable' or 'non_retriable'
            :param int max_retries: max retry count
            :return: created task record
        """
        if name is None:
            name = type_code
        vals = {
            'name': name,
            'type_code': type_code,
            'task_params': params or {},
            'channel': channel,
            'priority': priority,
            'timeout': timeout,
            'retry_policy': retry_policy,
            'max_retries': max_retries,
        }
        if eta:
            vals['eta'] = eta
        if parent_id:
            vals['parent_id'] = parent_id
        return self.create(vals)
