import logging
import uuid as _uuid_module
from datetime import timedelta

import psycopg2.errors

from odoo import models, fields, api, exceptions
from odoo.addons.generic_mixin.tools.x2m_agg_utils import read_counts_for_o2m

from ..exceptions import AlreadyScheduledException

_logger = logging.getLogger(__name__)

TASK_STATES = [
    ('pending', 'Pending'),
    ('assigned', 'Assigned'),
    ('running', 'Running'),
    ('stuck', 'Stuck'),
    ('waiting', 'Waiting for children'),
    ('done', 'Done'),
    ('failed', 'Failed'),
    ('cancelled', 'Cancelled'),
]

# Allowed state transitions: {from_state: [to_states]}
ALLOWED_TRANSITIONS = {
    'pending':   ['assigned', 'cancelled'],
    'assigned':  ['running', 'cancelled'],
    'running':   ['done', 'failed', 'waiting', 'stuck', 'cancelled'],
    'stuck':     ['done', 'failed', 'pending', 'cancelled'],
    'waiting':   ['done', 'failed', 'cancelled'],
    'failed':    ['pending', 'cancelled'],
    'done':      [],
    'cancelled': [],
}

RETRY_POLICIES = [
    ('no_retry', 'No Retry'),
    ('retry_known', 'Retry Known'),
    ('retry_any', 'Retry Any'),
]

# Map old policy names (pre-0.1.0) to current names for backward compat.
_RETRY_POLICY_ALIASES = {
    'non_retriable': 'no_retry',
    'retriable':     'retry_any',
}


def _normalize_retry_policy(value):
    """Translate deprecated policy names; warn so callers know to update.

    Extension modules not yet updated may still set
    ``_retry_policy = 'retriable'`` or pass
    ``retry_policy='non_retriable'`` to ``create_task()``.
    Accept these values rather than crashing, but warn so developers
    know to update their code.
    """
    canonical = _RETRY_POLICY_ALIASES.get(value)
    if canonical is not None:
        _logger.warning(
            "Deprecated retry_policy value %r — use %r instead. "
            "Old names will be removed in a future release.",
            value, canonical,
        )
        return canonical
    return value


TERMINAL_STATES = frozenset({'done', 'failed', 'cancelled'})

# States where a task is considered still active (not yet resolved)
ACTIVE_STATES = frozenset({
    'pending', 'assigned', 'running', 'stuck', 'waiting'})


class GenericTaskQueueTask(models.Model):
    _name = 'generic.task.queue.task'
    _description = 'Task Queue Task'
    _inherit = ['bus.listener.mixin']
    _order = 'priority, date_created'

    def init(self):
        """ Create partial indexes for claim_task and unique_key. """
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS
                generic_task_queue_task_claim_idx
            ON generic_task_queue_task
                (priority, date_created)
            WHERE state = 'pending'
        """)
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                generic_task_queue_task_unique_key_active_uniq
            ON generic_task_queue_task (unique_key)
            WHERE state IN (
                'pending', 'assigned', 'running', 'stuck', 'waiting'
            ) AND unique_key IS NOT NULL
        """)
        # Singleton NOT EXISTS guard: fast lookup of active tasks per type.
        # Includes 'stuck' so a timed-out singleton blocks new claims of
        # the same type while its thread is still alive.
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS
                generic_task_queue_task_singleton_active_idx
            ON generic_task_queue_task (type_code)
            WHERE state IN ('assigned', 'running', 'stuck')
        """)
        # Singleton canonical-task subquery: find first pending per type
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS
                generic_task_queue_task_singleton_pending_idx
            ON generic_task_queue_task (type_code, priority, date_created)
            WHERE state = 'pending'
        """)

    def _bus_channel(self):
        self.ensure_one()
        return self.create_uid.partner_id

    def _notify_state_change(self):
        """ Send gtq_task_update notification to the task creator.

            Called after every state transition. Fires via cr.postcommit
            (handled internally by bus.bus._sendone).
            Skipped if the creator has no partner (e.g. system users).
        """
        for task in self:
            partner = task.create_uid.partner_id
            if not partner:
                continue
            partner._bus_send('gtq_task_update', {
                'task_id': task.id,
                'state': task.state,
                'progress': task.progress,
            })

    def _notify_completion(self):
        """ Send a toast notification to the task creator when a task
            reaches a terminal state, if the task type opts in.

            Skipped if the creator has no partner (e.g. system users).
            Whether to notify for child tasks is left to the task type —
            set notify_on_completion = True only on types where it makes
            sense.
        """
        for task in self:
            if not task.type_id.notify_on_completion:
                continue
            partner = task.create_uid.partner_id
            if not partner:
                continue
            if task.state == 'done':
                notif_type = 'success'
                title = self.env._('Task completed')
            else:  # failed
                notif_type = 'danger'
                title = self.env._('Task failed')
            partner._bus_send('simple_notification', {
                'title': title,
                'message': task.name,
                'type': notif_type,
                'sticky': task.state == 'failed',
            })

    name = fields.Char(required=True, readonly=True)
    type_id = fields.Many2one(
        'generic.task.queue.task.type',
        required=True, index=True, ondelete='restrict', readonly=True,
        help="Task type that defines how to execute this task.")
    type_code = fields.Char(
        related='type_id.code', store=True, index=True,
        help="Dotted name of the task type "
             "(e.g. 'task.type.model.method')")
    state = fields.Selection(
        TASK_STATES, default='pending',
        required=True, index=True, readonly=True)
    channel = fields.Char(
        default='default', required=True, index=True, readonly=True,
        help="Routing channel. Workers only pick up tasks "
             "matching their channels.")
    priority = fields.Integer(
        default=5, readonly=True,
        help="Lower number = higher priority (0 is highest).")
    task_params = fields.Json(
        string='Task Parameters', default=dict, readonly=True,
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
    runner_id = fields.Char(
        readonly=True,
        help="UUID generated at claim time. Identifies the specific "
             "execution attempt. A task thread checks this before writing "
             "final state — if it no longer matches, the task was "
             "reassigned and the write is silently dropped (zombie-thread "
             "guard). Cleared when a task returns to pending.")
    eta = fields.Datetime(
        string='ETA', index=True, readonly=True,
        help="Earliest time this task should be executed. "
             "Leave empty for immediate execution.")
    retry_policy = fields.Selection(
        RETRY_POLICIES, default='no_retry', required=True, readonly=True)
    max_retries = fields.Integer(default=0, readonly=True)
    retry_count = fields.Integer(default=0, readonly=True)
    timeout = fields.Integer(
        default=0, readonly=True,
        help="Maximum execution time in seconds. "
             "0 means no timeout. Worker will mark the task "
             "as failed if execution exceeds this limit.")
    progress = fields.Integer(
        default=0, readonly=True,
        help="Execution progress (0-100).")
    parent_id = fields.Many2one(
        'generic.task.queue.task', index=True, ondelete='cascade',
        readonly=True,
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
    unique_key = fields.Char(
        index=True, readonly=True,
        help="Idempotency key. When set, only one task with this key "
             "may be active at a time. Pass on_conflict='reuse-running' "
             "(default) to get back the existing task, or "
             "on_conflict='raise' to raise AlreadyScheduledException."
    )

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

    @api.depends('child_ids')
    def _compute_child_count(self):
        mapped_data = read_counts_for_o2m(self, 'child_ids')
        for record in self:
            record.child_count = mapped_data.get(record.id, 0)

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
        self._notify_state_change()

    @api.private
    def action_stuck(self):
        """ Transition: running → stuck.

            Called by the worker when a task's timeout expires but its
            thread is still alive. Does NOT increment retry_count because
            the outcome of the thread is still unknown.
        """
        self._check_transition('stuck')
        self.write({'state': 'stuck'})
        self._notify_state_change()

    @api.private
    def action_done(self, result=None, runner_id=None):
        """ Transition: running/stuck → done.

            Called by worker (SUPERUSER context). No sudo needed.

            :param runner_id: If provided, the call is silently dropped
                when the task's current runner_id does not match.
                Prevents zombie threads from overwriting a task that
                has been reassigned to a new execution attempt.
        """
        if runner_id is not None:
            self.ensure_one()
            self.invalidate_recordset(['runner_id'])
            if self.runner_id != runner_id:
                _logger.info(
                    "Task %d: runner_id mismatch, dropping action_done "
                    "(zombie thread guard)", self.id)
                return
        self._check_transition('done')
        self.write({
            'state': 'done',
            'task_result': result,
            'date_completed': fields.Datetime.now(),
            'progress': 100,
        })
        self._notify_state_change()
        self._notify_completion()

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
        self._notify_state_change()

    @api.private
    def action_fail(self, error=None, error_data=None, runner_id=None):
        """ Transition: running/stuck/waiting → failed.

            Called by worker (SUPERUSER context). No sudo needed.

            :param str error: error message / traceback text
            :param dict error_data: structured error data (JSON)
            :param runner_id: If provided, the call is silently dropped
                when the task's current runner_id does not match.
                Prevents zombie threads from overwriting a reassigned task.
        """
        self.ensure_one()
        if runner_id is not None:
            self.invalidate_recordset(['runner_id'])
            if self.runner_id != runner_id:
                _logger.info(
                    "Task %d: runner_id mismatch, dropping action_fail "
                    "(zombie thread guard)", self.id)
                return
        self._check_transition('failed')
        vals = {
            'state': 'failed',
            'task_error': error,
            'date_completed': fields.Datetime.now(),
        }
        if error_data is not None:
            vals['task_error_data'] = error_data
        self.write(vals)
        self._notify_state_change()
        self._notify_completion()

    def action_retry(self, eta=None):
        """ Manual retry: transition failed → pending.

            Never increments retry_count — that is reserved for automatic
            retries by the worker. Manual retry is always allowed
            regardless of retry_policy or max_retries.
        """
        for record in self:
            if record.state != 'failed':
                raise exceptions.ValidationError(
                    self.env._("Only failed tasks can be retried."))
            record.sudo().write({
                'state': 'pending',
                'worker_id': False,
                'runner_id': False,
                'task_error': False,
                'progress': 0,
                'eta': eta,
            })
        self._notify_state_change()

    @api.private
    def _action_auto_retry(self, eta=None):
        """ Automatic retry: transition to pending and increment retry_count.

            Called by the worker when a task should be automatically retried
            (transient error or explicit RetryTask). The caller is responsible
            for checking retry_count < max_retries and policy before calling.
        """
        self.ensure_one()
        self.sudo().write({
            'state': 'pending',
            'worker_id': False,
            'runner_id': False,
            'task_error': False,
            'progress': 0,
            'eta': eta,
            'retry_count': self.retry_count + 1,
        })
        self._notify_state_change()

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
                'pending', 'assigned', 'running', 'stuck', 'waiting'))
        if children_to_cancel:
            children_to_cancel.action_cancel()
        self._notify_state_change()

    @api.private
    def _check_waiting_parent(self):
        """ Check if this waiting parent's children are all done.

            Called by the worker's poll loop for tasks in 'waiting'
            state. Transitions the parent to done or failed
            based on children's states.

            Uses SELECT FOR UPDATE SKIP LOCKED to prevent two workers
            (or two concurrent child-done notifications) from calling
            on_all_children_done() simultaneously on the same parent.
            If another transaction already holds the lock, this call
            is a no-op — the lock holder will complete the parent.
        """
        self.ensure_one()

        # Acquire a row-level lock before inspecting children.
        # SKIP LOCKED: if already locked by another transaction, return
        # immediately — that transaction is responsible for completing
        # this parent.
        self.flush_model()
        self.env.cr.execute(
            "SELECT id FROM generic_task_queue_task "
            "WHERE id = %s AND state = 'waiting' "
            "FOR UPDATE SKIP LOCKED",
            (self.id,))
        if not self.env.cr.fetchone():
            return

        # Re-read state from DB after acquiring lock (cache may be stale)
        self.invalidate_recordset(['state'])
        if self.state != 'waiting':
            return

        children = self.child_ids
        if not children:
            # No children — nothing to wait for
            self.action_done()
            return

        child_states = set(children.mapped('state'))

        # If any child is still in progress, keep waiting.
        # 'stuck' children are still potentially in progress.
        active_states = {'pending', 'assigned', 'running', 'stuck', 'waiting'}
        if child_states & active_states:
            return

        # All children are in terminal states (done/failed/cancelled)
        # Check if any child failed permanently
        failed_children = children.filtered(
            lambda c: c.state == 'failed')
        non_retriable_failures = failed_children.filtered(
            lambda c: (c.retry_policy != 'retry_any'
                       or c.retry_count >= c.max_retries))

        if non_retriable_failures:
            self.action_fail(
                "Child tasks failed: %s" % ', '.join(
                    non_retriable_failures.mapped('name')))
            return

        # If there are auto-retriable failures still pending retry,
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

            A gtq_task_progress bus notification is sent on the same
            cursor so it fires at the same commit, keeping the
            notification in sync with the data.

            If the task's type has ``propagate_progress = True`` and the
            task has a parent, the averaged progress of all siblings is
            written to the parent in the same cursor/commit, then the
            propagation recurses upward until a type with
            ``propagate_progress = False`` or a root task is reached.
        """
        value = max(0, min(100, int(value)))
        new_cr = self.pool.cursor()
        try:
            new_cr.execute(
                "UPDATE generic_task_queue_task "
                "SET progress = %s WHERE id IN %s",
                (value, tuple(self.ids)))
            # Collect partner ids via self (original env sees uncommitted
            # task rows). Then send bus notifications via new_env so they
            # queue on new_cr's postcommit and fire at the same commit as
            # the progress UPDATE — res.partner rows are always committed.
            new_env = api.Environment(new_cr, self.env.uid, {})
            for task in self:
                partner = task.create_uid.partner_id
                if not partner:
                    continue
                new_env['res.partner'].browse(partner.id)._bus_send(
                    'gtq_task_progress', {
                        'task_id': task.id,
                        'progress': value,
                    })
            # Propagate to parent when the task type opts in.
            # Checked via the main-env ORM (sees own uncommitted writes).
            # The actual SQL runs on new_cr so it commits atomically with
            # the progress write above.
            for task in self:
                if task.parent_id and task.type_id.propagate_progress:
                    self._propagate_progress_upward(
                        new_cr, new_env, task.parent_id.id)
            new_cr.commit()
        finally:
            new_cr.close()

    @api.private
    def _propagate_progress_upward(self, new_cr, new_env, parent_id):
        """ Average children's progress and write it to the parent task.

            Runs entirely on ``new_cr`` (the cursor already open in
            ``update_progress``) so all writes land in the same commit.

            Recurses if the parent's task type also has
            ``propagate_progress = True`` and the parent itself has a
            parent, allowing progress to bubble up through arbitrarily
            deep hierarchies in a single round-trip per level.

            :param new_cr: open cursor (from update_progress)
            :param new_env: Odoo environment bound to new_cr
            :param int parent_id: ID of the parent task to update
        """
        # Average progress across all committed siblings.
        # new_cr sees its own uncommitted writes (the child's just-updated
        # progress) as well as all previously committed sibling values.
        new_cr.execute("""
            SELECT COALESCE(AVG(progress), 0)::int, COUNT(*)
            FROM generic_task_queue_task
            WHERE parent_id = %s
        """, (parent_id,))
        avg_progress, child_count = new_cr.fetchone()
        if not child_count:
            return

        new_cr.execute("""
            UPDATE generic_task_queue_task
            SET progress = %s
            WHERE id = %s
        """, (avg_progress, parent_id))

        # Notify the parent task's creator via bus.
        new_cr.execute("""
            SELECT u.partner_id
            FROM generic_task_queue_task t
            JOIN res_users u ON u.id = t.create_uid
            WHERE t.id = %s
        """, (parent_id,))
        row = new_cr.fetchone()
        if row and row[0]:
            new_env['res.partner'].browse(row[0])._bus_send(
                'gtq_task_progress', {
                    'task_id': parent_id,
                    'progress': avg_progress,
                })

        # Recurse if the parent's type also opts in and has its own parent.
        new_cr.execute("""
            SELECT tt.propagate_progress, t.parent_id
            FROM generic_task_queue_task t
            LEFT JOIN generic_task_queue_task_type tt
                   ON tt.id = t.type_id
            WHERE t.id = %s
        """, (parent_id,))
        row = new_cr.fetchone()
        if row and row[0] and row[1]:
            self._propagate_progress_upward(new_cr, new_env, row[1])

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
    def claim_task(self, worker, channels, task_types,
                   singleton_types=None, limit=1):
        """ Atomically claim pending tasks for a worker.

            Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent
            race conditions between concurrent workers.

            :param worker: generic.task.queue.worker record
            :param list channels: channels this worker handles
            :param list task_types: task type codes this worker handles
                (empty = all types)
            :param frozenset singleton_types: type codes that may have at
                most one assigned/running task cluster-wide; tasks of these
                types are skipped when another task of the same type is
                already executing
            :param int limit: max number of tasks to claim
            :return: recordset of claimed tasks
        """
        if not channels:
            return self.browse()
        # Flush pending ORM writes so raw SQL sees current state
        self.flush_model()

        # NULL sentinels allow a single static query for both cases:
        #   type_filter=None  → (%s IS NULL) short-circuits → accept all types
        #   type_filter=[...] → filter by type_code = ANY(array)
        #   singleton_filter=None  → (%s IS NULL) → singleton clause is no-op
        #   singleton_filter=[...] → enforce NOT EXISTS guard
        type_filter = list(task_types) if task_types else None
        singleton_filter = (
            list(singleton_types) if singleton_types else None)

        self.env.cr.execute(
            """
            SELECT id FROM generic_task_queue_task
            WHERE state = 'pending'
              AND channel = ANY(%s)
              AND (%s IS NULL OR type_code = ANY(%s))
              AND (eta IS NULL OR eta <= (NOW() AT TIME ZONE 'UTC'))
              AND (
                  %s IS NULL
                  OR NOT (type_code = ANY(%s))
                  OR NOT EXISTS (
                      SELECT 1 FROM generic_task_queue_task t2
                      WHERE t2.type_code = generic_task_queue_task.type_code
                        AND t2.state IN ('assigned', 'running', 'stuck')
                  )
              )
              AND (
                  %s IS NULL
                  OR NOT (type_code = ANY(%s))
                  OR id = (
                      SELECT t3.id
                      FROM generic_task_queue_task t3
                      WHERE t3.type_code = generic_task_queue_task.type_code
                        AND t3.state = 'pending'
                      ORDER BY t3.priority, t3.date_created
                      LIMIT 1
                  )
              )
            ORDER BY priority, date_created
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (
                list(channels),
                type_filter, type_filter,
                singleton_filter, singleton_filter,
                singleton_filter, singleton_filter,
                limit,
            )
        )
        task_ids = [r[0] for r in self.env.cr.fetchall()]
        if task_ids:
            tasks = self.browse(task_ids)
            tasks.action_assign(worker)
            # Assign a fresh runner_id to each task so the executing
            # thread can detect reassignment (zombie-thread guard).
            for task in tasks:
                task.write({'runner_id': str(_uuid_module.uuid4())})
            return tasks
        return self.browse()

    @api.model
    def create_task(self, type_code, name=None, params=None,
                    channel=None, priority=5, eta=None,
                    timeout=0, parent_id=None,
                    retry_policy=None, max_retries=None,
                    unique_key=None, on_conflict='reuse-running'):
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
            :param str channel: routing channel. When omitted, falls back to
                the task type's ``_default_channel`` (default: ``'default'``).
            :param int priority: 0 = highest
            :param datetime eta: earliest execution time
            :param int timeout: max execution seconds (0 = no limit)
            :param int parent_id: parent task ID for sub-tasks
            :param str retry_policy: 'no_retry', 'retry_known', or 'retry_any'.
                Defaults to the task type's ``_retry_policy`` class attribute.
            :param int max_retries: max retry count.
                Defaults to the task type's ``_max_retries`` class attribute.
            :param str unique_key: idempotency key. When set, only one task
                with this key may be active at a time.
            :param str on_conflict: what to do when a task with the same
                unique_key is already active. ``'reuse-running'`` (default)
                returns the existing task; ``'raise'`` raises
                ``AlreadyScheduledException``.
            :return: created task record
        """
        if retry_policy is None or max_retries is None or channel is None:
            from ..service.task_type_registry import TaskTypeRegistry
            try:
                cls = TaskTypeRegistry().get_task_type(type_code)
                if retry_policy is None:
                    retry_policy = cls._retry_policy
                if max_retries is None:
                    max_retries = cls._max_retries
                if channel is None:
                    channel = cls._default_channel
            except KeyError:
                if retry_policy is None:
                    retry_policy = 'no_retry'
                if max_retries is None:
                    max_retries = 0
                if channel is None:
                    channel = 'default'
        retry_policy = _normalize_retry_policy(retry_policy)
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
        if not unique_key:
            return self.create(vals)

        vals['unique_key'] = unique_key

        # Fast path: existing active task with this key.
        # FOR UPDATE serialises against a concurrent transaction that may
        # be in the middle of creating the same key.
        self.flush_model()
        self.env.cr.execute(
            "SELECT id FROM generic_task_queue_task "
            "WHERE unique_key = %s AND state IN %s "
            "FOR UPDATE",
            (unique_key, tuple(ACTIVE_STATES))
        )
        row = self.env.cr.fetchone()
        if row:
            existing = self.browse(row[0])
            if on_conflict == 'raise':
                raise AlreadyScheduledException(existing)
            return existing

        # No active task found — create one. Use a savepoint (flush=False,
        # to avoid wiping the ORM cache on rollback) so that a concurrent
        # INSERT that beats us to the partial unique index is handled
        # gracefully rather than aborting the whole transaction.
        try:
            with self.env.cr.savepoint(flush=False):
                return self.create(vals)
        except psycopg2.errors.UniqueViolation:
            # Another transaction inserted the same unique_key between our
            # SELECT and our INSERT. Find the winner and apply the policy.
            self.env.cr.execute(
                "SELECT id FROM generic_task_queue_task "
                "WHERE unique_key = %s AND state IN %s",
                (unique_key, tuple(ACTIVE_STATES))
            )
            row = self.env.cr.fetchone()
            if row:
                existing = self.browse(row[0])
                if on_conflict == 'raise':
                    raise AlreadyScheduledException(existing)
                return existing
            raise  # unexpected — re-raise original UniqueViolation

    @api.model
    def _gc_tasks(self):
        """ Delete old terminal tasks. Called by the vacuum cron job.

            Retention period and batch size are configurable via
            System Parameters:
              generic_task_queue.vacuum_days       (default: 30)
              generic_task_queue.vacuum_batch_size (default: 1000)

            Set vacuum_days to 0 to disable cleanup entirely.

            Only root tasks (parent_id = False) are searched; children
            are removed automatically via their ondelete='cascade'.
        """
        get_param = self.env['ir.config_parameter'].sudo().get_param
        days = int(get_param('generic_task_queue.vacuum_days', 30))
        if days <= 0:
            return
        batch_size = int(
            get_param('generic_task_queue.vacuum_batch_size', 1000))
        cutoff = fields.Datetime.now() - timedelta(days=days)
        tasks = self.sudo().search([
            ('state', 'in', ['done', 'failed', 'cancelled']),
            ('date_completed', '<', cutoff),
            ('parent_id', '=', False),
        ], limit=batch_size)
        if tasks:
            _logger.info(
                "Vacuuming %d completed tasks older than %d days",
                len(tasks), days)
            tasks.unlink()
