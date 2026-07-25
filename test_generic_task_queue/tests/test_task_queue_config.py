import os
import tempfile
import textwrap
import unittest.mock as mock

from odoo.tests.common import TransactionCase

from odoo.addons.generic_task_queue.service.task_queue_service import (
    _get_task_queue_config,
)


class TestGetTaskQueueConfig(TransactionCase):
    """Tests for _get_task_queue_config().

    Odoo 19 removed config.misc / config.get_misc, so the
    [generic_task_queue] section is read directly from the active config file.
    """

    def _write_conf(self, body):
        fd, path = tempfile.mkstemp(suffix='.conf')
        with os.fdopen(fd, 'w') as fh:
            fh.write(textwrap.dedent(body))
        self.addCleanup(os.unlink, path)
        return path

    def _patch_config_path(self, path):
        from odoo.tools import config as odoo_config
        return mock.patch.object(odoo_config, 'get', return_value=path)

    def test_returns_empty_when_no_section(self):
        path = self._write_conf(
            """
            [options]
            db_name = test
            """)
        with self._patch_config_path(path):
            self.assertEqual(_get_task_queue_config(), {})

    def test_reads_section_from_config_file(self):
        path = self._write_conf(
            """
            [options]
            db_name = test
            [generic_task_queue]
            my_service_max_parallel_jobs = 4
            """)
        with self._patch_config_path(path):
            cfg = _get_task_queue_config()
        self.assertEqual(cfg.get('my_service_max_parallel_jobs'), '4')

    def test_returns_empty_when_no_config_file(self):
        with self._patch_config_path(None):
            self.assertEqual(_get_task_queue_config(), {})
