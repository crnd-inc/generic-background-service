from odoo.addons.generic_background_service import (
    AbstractBackgroundServiceWorker,
)


class TestBGServiceWorkerComputePower(AbstractBackgroundServiceWorker):

    def get_sleep_timeout(self):
        return 5.0

    def run_service(self):
        with self.with_env() as env:
            todo = env['test.bg.service.compute.power'].search([
                ('date_completed', '=', False)
            ], limit=5)
            todo.do_job()
