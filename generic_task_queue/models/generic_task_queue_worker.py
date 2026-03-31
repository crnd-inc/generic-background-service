import logging

from datetime import timedelta

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# How long without a heartbeat before a worker is considered stale
DEFAULT_HEARTBEAT_TIMEOUT = 60  # seconds


class GenericTaskQueueWorker(models.Model):
    _name = 'generic.task.queue.worker'
    _description = 'Task Queue Worker'

    name = fields.Char(compute='_compute_name', store=True)
    uuid = fields.Char(
        required=True, index=True, readonly=True)
    service_name = fields.Char(index=True)
    dbname = fields.Char(index=True)
    state = fields.Selection([
        ('active', 'Active'),
        ('stale', 'Stale'),
        ('dead', 'Dead'),
    ], default='active', required=True, index=True)
    last_heartbeat = fields.Datetime()
    channels = fields.Char(
        help="Comma-separated list of channels this worker handles.")
    task_types = fields.Char(
        help="Comma-separated list of task type codes "
             "this worker handles.")
    max_parallel_jobs = fields.Integer(default=1)
    date_registered = fields.Datetime(
        default=fields.Datetime.now, readonly=True)

    _sql_constraints = [
        ('uuid_uniq', 'UNIQUE (uuid)', 'Worker UUID must be unique.'),
    ]

    def _compute_name(self):
        for record in self:
            record.name = "Worker %s (%s)" % (
                record.uuid[:8] if record.uuid else '?',
                record.service_name or 'unknown')

    def heartbeat(self):
        """Update heartbeat timestamp. Reactivate if stale."""
        self.write({
            'last_heartbeat': fields.Datetime.now(),
            'state': 'active',
        })

    def mark_dead(self):
        """Mark worker as dead and reassign its retriable tasks."""
        self.write({'state': 'dead'})
        Task = self.env['generic.task.queue.task']
        stuck_tasks = Task.search([
            ('worker_id', 'in', self.ids),
            ('state', 'in', ('assigned', 'running')),
        ])
        for task in stuck_tasks:
            if task.retry_policy == 'retriable':
                task.write({
                    'state': 'pending',
                    'worker_id': False,
                })
            else:
                task.write({
                    'state': 'failed',
                    'task_error': 'Worker died during execution',
                })

    @api.model
    def find_or_create(self, service_name, dbname, uuid,
                       channels, task_types, max_parallel_jobs):
        """Find existing worker record for this service+db,
        or create a new one.

        Reuses existing records to avoid table bloat from
        repeated restarts.
        """
        existing = self.search([
            ('service_name', '=', service_name),
            ('dbname', '=', dbname),
        ], limit=1)
        vals = {
            'uuid': uuid,
            'state': 'active',
            'last_heartbeat': fields.Datetime.now(),
            'channels': channels,
            'task_types': task_types,
            'max_parallel_jobs': max_parallel_jobs,
        }
        if existing:
            existing.write(vals)
            return existing
        vals.update({
            'service_name': service_name,
            'dbname': dbname,
        })
        return self.create(vals)

    @api.model
    def check_stale_workers(self, heartbeat_timeout=None):
        """Find workers that missed their heartbeat and handle them.

        Called periodically by active workers to detect dead peers.
        """
        if heartbeat_timeout is None:
            heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT
        threshold = fields.Datetime.now() - timedelta(
            seconds=heartbeat_timeout)
        stale_workers = self.search([
            ('state', '=', 'active'),
            ('last_heartbeat', '<', threshold),
        ])
        if stale_workers:
            _logger.warning(
                "Marking %d stale workers as dead: %s",
                len(stale_workers),
                ', '.join(
                    w.name or w.uuid or str(w.id)
                    for w in stale_workers))
            stale_workers.mark_dead()
        return stale_workers
