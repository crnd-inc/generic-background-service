from odoo.tests import tagged

from odoo.addons.generic_background_service.tests.common import (
    BackgroundServiceTestCase,
)
from odoo.addons.test_generic_background_service.service.test_bg_worker_compute_power import (  # noqa: E501
    TestBGServiceWorkerComputePower,
)


@tagged('post_install', '-at_install')
class TestComputePowerWorker(BackgroundServiceTestCase):
    """End-to-end test of the compute power background service
    using the test framework.

    This demonstrates how addon developers can test their workers:
    1. Create records in the database
    2. Run the worker for one cycle
    3. Verify the records were processed
    """

    def test_worker_computes_results(self):
        """Worker should compute base**power for pending records."""
        Job = self.env['test.bg.service.compute.power']
        job1 = Job.create({'base': 2.0, 'power': 10.0})
        job2 = Job.create({'base': 3.0, 'power': 3.0})

        self.assertFalse(job1.date_completed)
        self.assertFalse(job2.date_completed)

        self.run_worker_once(TestBGServiceWorkerComputePower)

        self.assertEqual(job1.result, 1024.0)
        self.assertTrue(job1.date_completed)
        self.assertEqual(job2.result, 27.0)
        self.assertTrue(job2.date_completed)

    def test_worker_skips_completed_records(self):
        """Worker should not reprocess already completed records."""
        Job = self.env['test.bg.service.compute.power']
        from odoo import fields
        job = Job.create({
            'base': 5.0,
            'power': 2.0,
            'result': 25.0,
            'date_completed': fields.Datetime.now(),
        })

        self.run_worker_once(TestBGServiceWorkerComputePower)

        # Result should remain unchanged
        self.assertEqual(job.result, 25.0)

    def test_worker_handles_empty_queue(self):
        """Worker should handle gracefully when there are
        no pending records."""
        # No records created - just verify no error
        self.run_worker_once(TestBGServiceWorkerComputePower)

    def test_worker_batch_limit(self):
        """Worker processes at most 5 records per cycle
        (as defined in the worker's search limit)."""
        Job = self.env['test.bg.service.compute.power']
        jobs = Job
        for i in range(8):
            jobs |= Job.create({'base': 2.0, 'power': float(i)})

        # Run one cycle - should process at most 5
        self.run_worker_once(TestBGServiceWorkerComputePower)

        completed = jobs.filtered(lambda j: j.date_completed)
        pending = jobs.filtered(lambda j: not j.date_completed)
        self.assertEqual(len(completed), 5)
        self.assertEqual(len(pending), 3)

        # Run another cycle - should process remaining 3
        self.run_worker_once(TestBGServiceWorkerComputePower)

        completed = jobs.filtered(lambda j: j.date_completed)
        self.assertEqual(len(completed), 8)
