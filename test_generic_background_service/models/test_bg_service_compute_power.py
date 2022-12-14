from odoo import models, fields


class TestBGServiceComputePower(models.Model):
    _name = 'test.bg.service.compute.power'
    _inherit = [
        'generic.mixin.refresh.view',
    ]
    _description = 'Test BG Service: Compute Power'

    date_created = fields.Datetime(
        required=True, index=True, default=fields.Datetime.now)
    date_completed = fields.Datetime(index=True)
    base = fields.Float()
    power = fields.Float()
    result = fields.Float()

    def do_job(self):
        for record in self:
            record.result = record.base ** record.power
            record.date_completed = fields.Datetime.now()
