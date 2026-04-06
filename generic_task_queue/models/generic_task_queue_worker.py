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
    hostname = fields.Char(
        index=True, readonly=True,
        help="Hostname of the machine running this worker. "
             "Used to give each machine its own worker record in "
             "multi-instance deployments.")
    state = fields.Selection([
        ('active', 'Active'),
        ('stuck', 'Stuck'),
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
            record.name = "%s @ %s" % (
                record.service_name or 'unknown',
                record.hostname or 'unknown')

    @api.private
    def heartbeat(self, stuck=False):
        """Update heartbeat timestamp and reflect current stuck state.

        :param bool stuck: True when all worker slots are occupied by
            timed-out threads (worker cannot claim new tasks). The DB
            state is kept in sync each heartbeat so it persists across
            loop iterations rather than being overwritten immediately.
        """
        self.write({
            'last_heartbeat': fields.Datetime.now(),
            'state': 'stuck' if stuck else 'active',
        })

    @api.private
    def mark_dead(self):
        """Mark worker as dead and reassign its in-flight tasks.

        Handles tasks in assigned, running, and stuck states.
        For stuck tasks (timeout expired, thread was still alive),
        retry_count is incremented because an execution was attempted.
        runner_id is cleared on pending tasks to invalidate any zombie
        threads still holding the old runner_id.
        """
        self.write({'state': 'dead'})
        Task = self.env['generic.task.queue.task']
        orphaned = Task.search([
            ('worker_id', 'in', self.ids),
            ('state', 'in', ('assigned', 'running', 'stuck')),
        ])
        for task in orphaned:
            if task.retry_policy == 'retriable':
                task.write({
                    'state': 'pending',
                    'worker_id': False,
                    'runner_id': False,
                })
            else:
                task.write({
                    'state': 'failed',
                    'task_error': 'Worker died during execution',
                    'retry_count': task.retry_count + 1,
                })

    @api.private
    def mark_stuck(self):
        """Mark worker as stuck (all slots occupied by timed-out threads).

        The worker continues heartbeating but is making no progress.
        In worker mode this is a transient state before self-restart.
        In threaded mode it signals that manual intervention is required.
        """
        self.write({'state': 'stuck'})

    @api.private
    @api.model
    def find_or_create(self, service_name, dbname, uuid,
                       channels, task_types, max_parallel_jobs,
                       hostname=None):
        """Find existing worker record for this service+db+hostname,
        or create a new one.

        Each (service_name, dbname, hostname) triple gets its own record,
        so machines in a multi-instance deployment never clobber each
        other's UUID. Records are reused across restarts of the same
        service on the same machine.
        """
        domain = [
            ('service_name', '=', service_name),
            ('dbname', '=', dbname),
            ('hostname', '=', hostname),
        ]
        existing = self.search(domain, limit=1)
        vals = {
            'uuid': uuid,
            'state': 'active',
            'last_heartbeat': fields.Datetime.now(),
            'channels': channels,
            'task_types': task_types,
            'max_parallel_jobs': max_parallel_jobs,
            'hostname': hostname,
        }
        if existing:
            existing.write(vals)
            return existing
        vals.update({
            'service_name': service_name,
            'dbname': dbname,
        })
        return self.create(vals)

    @api.private
    @api.model
    def check_stale_workers(self, heartbeat_timeout=None):
        """Find workers that missed their heartbeat and handle them.

        Uses FOR UPDATE SKIP LOCKED to prevent multiple workers
        from processing the same stale peer simultaneously.

        Called periodically by active workers to detect dead peers.

        Both 'active' and 'stuck' workers are checked: a stuck worker
        may have entered the stuck state and then died (SIGKILL, OOM)
        without ever reactivating — its tasks must still be reassigned.
        """
        if heartbeat_timeout is None:
            heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT
        threshold = fields.Datetime.now() - timedelta(
            seconds=heartbeat_timeout)
        self.env.cr.execute("""
            SELECT id FROM generic_task_queue_worker
            WHERE state IN ('active', 'stuck')
              AND last_heartbeat < %s
            FOR UPDATE SKIP LOCKED
        """, (threshold,))
        worker_ids = [r[0] for r in self.env.cr.fetchall()]
        if worker_ids:
            stale_workers = self.browse(worker_ids)
            _logger.warning(
                "Marking %d stale workers as dead: %s",
                len(stale_workers),
                ', '.join(
                    w.name or w.uuid or str(w.id)
                    for w in stale_workers))
            stale_workers.mark_dead()
            return stale_workers
        return self.browse()
