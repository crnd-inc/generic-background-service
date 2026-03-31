from odoo import models, fields


class TestTaskTarget(models.Model):
    _name = 'test.task.target'
    _description = 'Test Task Target'

    name = fields.Char(required=True)
    value = fields.Integer(default=0)
    processed = fields.Boolean(default=False)

    def do_increment(self, amount=1):
        """Test method: increment value by amount."""
        for record in self:
            record.value += amount
            record.processed = True

    def action_plan_task(self):
        """Create a background task to increment this record."""
        task = self._g_task_queue__plan('do_increment', amount=1)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'generic.task.queue.task',
            'res_id': task.id,
            'view_mode': 'form',
        }
