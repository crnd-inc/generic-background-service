from odoo.addons.generic_background_service import BackgroundService

from .test_bg_worker_compute_power import TestBGServiceWorkerComputePower


class TestBGServiceComputePower(BackgroundService):
    _name = 'test.bg.service.compute.power'

    def get_worker_class(self):
        return TestBGServiceWorkerComputePower
