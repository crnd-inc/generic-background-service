from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    gtq_vacuum_days = fields.Integer(
        string='Task Retention (days)',
        default=7,
        config_parameter='generic_task_queue.vacuum_days',
        help="Number of days to keep completed, failed, and cancelled tasks. "
             "Set to 0 to disable automatic cleanup.",
    )
    gtq_vacuum_batch_size = fields.Integer(
        string='Vacuum Batch Size',
        default=1000,
        config_parameter='generic_task_queue.vacuum_batch_size',
        help="Maximum number of tasks deleted per vacuum cron run.",
    )
