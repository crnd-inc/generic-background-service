import logging

from odoo import models, fields, api, exceptions

_logger = logging.getLogger(__name__)

TASK_STATES = [
    ('pending', 'Pending'),
    ('assigned', 'Assigned'),
    ('running', 'Running'),
    ('waiting', 'Waiting for children'),
    ('done', 'Done'),
    ('failed', 'Failed'),
    ('cancelled', 'Cancelled'),
]

# Allowed state transitions: {from_state: [to_states]}
ALLOWED_TRANSITIONS = {
    'pending': ['assigned', 'cancelled'],
    'assigned': ['running', 'cancelled'],
    'running': ['done', 'failed', 'waiting', 'cancelled'],
    'waiting': ['done', 'failed', 'cancelled'],
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
    type_id = fields.Many2one(
        'generic.task.queue.task.type',
        required=True, index=True, ondelete='restrict',
        help="Task type that defines how to execute this task.")
    type_code = fields.Char(
        related='type_id.code', store=True, index=True,
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
    task_error_data = fields.Json(
        readonly=True,
        help="Structured error data (JSON). Use for programmatic "
             "error handling alongside task_error text.")
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
    child_count = fields.Integer(
        compute='_compute_child_count', string='Sub-task Count')
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
        """ Resolve type_code to type_id if needed. """
        TaskType = self.env['generic.task.queue.task.type']
        for vals in vals_list:
            if 'type_id' not in vals and 'type_code' in vals:
                # Resolve type_code string to type_id
                code = vals.pop('type_code')
                task_type = TaskType.search([
                    ('code', '=', code),
                    ('active', '=', True),
                ], limit=1)
                if not task_type:
                    raise exceptions.ValidationError(
                        self.env._(
                            "Unknown task type '%(type_code)s'.",
                            type_code=code,
                        ))
                vals['type_id'] = task_type.id
        return super().create(vals_list)

    def _compute_child_count(self):
        for record in self:
            record.child_count = len(record.child_ids)

    def action_open_child_tasks(self):
        """ Open child tasks in a list view. """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Sub-tasks: %s' % self.name,
            'res_model': 'generic.task.queue.task',
            'view_mode': 'list,form',
            'domain': [('parent_id', '=', self.id)],
        }

    def action_open_parent_task(self):
        """ Open the parent task in form view. """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Parent Task',
            'res_model': 'generic.task.queue.task',
            'res_id': self.parent_id.id,
            'view_mode': 'form',
        }

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
    def action_wait_children(self):
        """ Transition: running → waiting.

            Called by task types that spawn children and need
            to wait for all of them to complete before finishing.
        """
        self._check_transition('waiting')
        self.write({
            'state': 'waiting',
        })

    @api.private
    def action_fail(self, error=None, error_data=None):
        """ Transition: running/waiting → failed.

            Called by worker (SUPERUSER context). No sudo needed.

            :param str error: error message / traceback text
            :param dict error_data: structured error data (JSON)
        """
        self.ensure_one()
        self._check_transition('failed')
        vals = {
            'state': 'failed',
            'task_error': error,
            'date_completed': fields.Datetime.now(),
            'retry_count': self.retry_count + 1,
        }
        if error_data is not None:
            vals['task_error_data'] = error_data
        self.write(vals)

    def action_retry(self):
        """ Transition: failed → pending (if retriable).

            Callable by task owner from UI. Uses sudo() to write
            protected fields.

            Manual retry is always allowed regardless of
            max_retries — the limit only applies to automatic
            retries by the worker.
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
            lambda c: c.state in (
                'pending', 'assigned', 'running', 'waiting'))
        if children_to_cancel:
            children_to_cancel.action_cancel()

    @api.private
    def _check_waiting_parent(self):
        """ Check if this waiting parent's children are all done.

            Called by the worker's poll loop for tasks in 'waiting'
            state. Transitions the parent to done or failed
            based on children's states.
        """
        self.ensure_one()
        if self.state != 'waiting':
            return

        children = self.child_ids
        if not children:
            # No children — nothing to wait for
            self.action_done()
            return

        child_states = set(children.mapped('state'))

        # If any child is still in progress, keep waiting
        active_states = {'pending', 'assigned', 'running', 'waiting'}
        if child_states & active_states:
            return

        # All children are in terminal states (done/failed/cancelled)
        # Check if any child failed permanently
        failed_children = children.filtered(
            lambda c: c.state == 'failed')
        non_retriable_failures = failed_children.filtered(
            lambda c: (c.retry_policy != 'retriable'
                       or c.retry_count >= c.max_retries))

        if non_retriable_failures:
            self.action_fail(
                "Child tasks failed: %s" % ', '.join(
                    non_retriable_failures.mapped('name')))
            return

        # If there are retriable failures still pending retry,
        # keep waiting
        if failed_children:
            return

        # All children done (or cancelled) — call the hook
        # and complete the parent
        from ..service.task_type_registry import TaskTypeRegistry
        registry = TaskTypeRegistry()
        try:
            task_type_cls = registry.get_task_type(self.type_code)
            task_type = task_type_cls()
            result = task_type.on_all_children_done(self.env, self)
        except KeyError:
            result = None
        except Exception:
            _logger.error(
                "Error in on_all_children_done for task %d",
                self.id, exc_info=True)
            result = None

        self.action_done(result)

    @api.private
    @api.model
    def create_children(self, parent_task, type_code, params_list,
                        **common_vals):
        """ Create multiple child tasks for a parent task.

            :param parent_task: parent task record
            :param str type_code: task type for all children
            :param list params_list: list of task_params dicts
            :param common_vals: common field values applied
                to all children (e.g., channel, priority)
            :return: recordset of created child tasks
        """
        vals_list = []
        for i, params in enumerate(params_list):
            vals = {
                'name': '%s [%d/%d]' % (
                    parent_task.name, i + 1, len(params_list)),
                'type_code': type_code,
                'parent_id': parent_task.id,
                'task_params': params,
                'channel': parent_task.channel,
            }
            vals.update(common_vals)
            vals_list.append(vals)
        return self.create(vals_list)

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
        if not channels:
            return self.browse()
        # Flush pending ORM writes so raw SQL sees current state
        self.flush_model()

        # Build query — skip type filter if task_types is empty
        # (empty means "all types")
        if task_types:
            self.env.cr.execute("""
                SELECT id FROM generic_task_queue_task
                WHERE state = 'pending'
                  AND channel IN %s
                  AND type_code IN %s
                  AND (eta IS NULL
                       OR eta <= (NOW() AT TIME ZONE 'UTC'))
                ORDER BY priority, date_created
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            """, (tuple(channels), tuple(task_types), limit))
        else:
            self.env.cr.execute("""
                SELECT id FROM generic_task_queue_task
                WHERE state = 'pending'
                  AND channel IN %s
                  AND (eta IS NULL
                       OR eta <= (NOW() AT TIME ZONE 'UTC'))
                ORDER BY priority, date_created
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            """, (tuple(channels), limit))
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
