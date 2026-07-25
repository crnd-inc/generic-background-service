import resource
import signal
import unittest.mock as mock

from odoo.tests.common import TransactionCase

from odoo.addons.generic_background_service.service.background_service import (
    BackgroundService,
)
from odoo.addons.generic_background_service.service.background_service_container import (  # noqa: E501
    DEFAULT_LIMIT_TIME_CPU_BACKGROUND,
    _get_background_service_config,
    wrap_service_as_worker,
)


class _DummyService(BackgroundService):
    _name = 'test.container.worker.limits.service'

    def get_worker_class(self):
        pass

    def run(self):
        pass

    def stop(self):
        pass


def _make_worker(limit_time_cpu=None):
    """Build a ServiceContainerWorker instance with a mocked multi object."""
    ContainerWorker = wrap_service_as_worker(_DummyService)

    multi = mock.MagicMock()
    multi.timeout = 60
    multi.limit_request = 8192
    multi.pipe_new.return_value = (mock.MagicMock(), mock.MagicMock())

    with mock.patch(
        'odoo.addons.generic_background_service'
        '.service.background_service_container'
        '._get_background_service_config',
        return_value={
            'limit_time_cpu': (
                limit_time_cpu
                if limit_time_cpu is not None
                else DEFAULT_LIMIT_TIME_CPU_BACKGROUND
            ),
        },
    ):
        worker = ContainerWorker(multi)

    worker.pid = 12345
    return worker


class TestGetBackgroundServiceConfig(TransactionCase):
    """Tests for the _get_background_service_config() helper."""

    def _patch_raw(self, value):
        # Odoo 19 removed config.misc; the section is read from the config
        # file via _read_background_service_config(), so patch that helper.
        return mock.patch(
            'odoo.addons.generic_background_service.service'
            '.background_service_container._read_background_service_config',
            return_value=value,
        )

    def test_returns_default_when_no_section(self):
        with self._patch_raw({}):
            cfg = _get_background_service_config()
        self.assertEqual(
            cfg['limit_time_cpu'], DEFAULT_LIMIT_TIME_CPU_BACKGROUND)

    def test_reads_custom_limit_time_cpu(self):
        with self._patch_raw({'limit_time_cpu': '7200'}):
            cfg = _get_background_service_config()
        self.assertEqual(cfg['limit_time_cpu'], 7200)

    def test_invalid_value_falls_back_to_default(self):
        with self._patch_raw({'limit_time_cpu': 'not_a_number'}):
            cfg = _get_background_service_config()
        self.assertEqual(
            cfg['limit_time_cpu'], DEFAULT_LIMIT_TIME_CPU_BACKGROUND)

    def test_invalid_value_does_not_raise(self):
        with self._patch_raw({'limit_time_cpu': None}):
            try:
                _get_background_service_config()
            except Exception as exc:
                self.fail(
                    "_get_background_service_config() raised"
                    " unexpectedly: %s" % exc)

    def test_reads_section_from_config_file(self):
        """End-to-end: the [generic_background_service] section is read from
        the active odoo.cfg file (Odoo 19 no longer parses custom sections)."""
        import os
        import tempfile
        import textwrap

        from odoo.tools import config as odoo_config

        fd, path = tempfile.mkstemp(suffix='.conf')
        try:
            with os.fdopen(fd, 'w') as fh:
                fh.write(textwrap.dedent(
                    """
                    [options]
                    db_name = test
                    [generic_background_service]
                    limit_time_cpu = 7200
                    """))
            with mock.patch.object(odoo_config, 'get', return_value=path):
                cfg = _get_background_service_config()
            self.assertEqual(cfg['limit_time_cpu'], 7200)
        finally:
            os.unlink(path)


class TestServiceContainerWorkerCPULimit(TransactionCase):
    """Tests for _reset_cpu_limit() behaviour."""

    def test_reset_cpu_limit_calls_setrlimit(self):
        worker = _make_worker(limit_time_cpu=3600)

        fake_rusage = mock.MagicMock()
        fake_rusage.ru_utime = 10.0
        fake_rusage.ru_stime = 5.0  # current cpu = 15s

        with (
            mock.patch('resource.getrusage', return_value=fake_rusage),
            mock.patch(
                'resource.getrlimit',
                return_value=(60, resource.RLIM_INFINITY)),
            mock.patch('resource.setrlimit') as mock_setrlimit,
        ):
            worker._reset_cpu_limit()

        mock_setrlimit.assert_called_once_with(
            resource.RLIMIT_CPU, (15 + 3600, resource.RLIM_INFINITY))

    def test_reset_cpu_limit_respects_hard_limit(self):
        worker = _make_worker(limit_time_cpu=3600)

        fake_rusage = mock.MagicMock()
        fake_rusage.ru_utime = 0.0
        fake_rusage.ru_stime = 0.0

        # hard limit lower than our desired soft limit
        with (
            mock.patch('resource.getrusage', return_value=fake_rusage),
            mock.patch('resource.getrlimit', return_value=(60, 100)),
            mock.patch('resource.setrlimit') as mock_setrlimit,
        ):
            worker._reset_cpu_limit()

        # new_soft would be 3600, but hard is 100 — must clamp to hard
        mock_setrlimit.assert_called_once_with(resource.RLIMIT_CPU, (100, 100))

    def test_check_limits_restores_large_cpu_limit(self):
        worker = _make_worker(limit_time_cpu=3600)

        fake_rusage = mock.MagicMock()
        fake_rusage.ru_utime = 0.0
        fake_rusage.ru_stime = 0.0

        with (
            mock.patch.object(
                worker.__class__.__bases__[0],
                'check_limits',
                return_value=None),
            mock.patch('resource.getrusage', return_value=fake_rusage),
            mock.patch(
                'resource.getrlimit',
                return_value=(60, resource.RLIM_INFINITY)),
            mock.patch('resource.setrlimit') as mock_setrlimit,
        ):
            worker.check_limits()

        mock_setrlimit.assert_called_once_with(
            resource.RLIMIT_CPU, (3600, resource.RLIM_INFINITY))


class TestServiceContainerWorkerGracefulCPUShutdown(TransactionCase):
    """Tests for signal_time_expired_handler()."""

    def test_handler_sets_alive_false(self):
        worker = _make_worker()
        worker.alive = True
        worker.signal_time_expired_handler(signal.SIGXCPU, None)
        self.assertFalse(worker.alive)

    def test_handler_calls_service_stop(self):
        worker = _make_worker()
        worker.alive = True
        with mock.patch.object(worker.service, 'stop') as mock_stop:
            worker.signal_time_expired_handler(signal.SIGXCPU, None)
        mock_stop.assert_called_once()

    def test_handler_does_not_raise(self):
        worker = _make_worker()
        try:
            worker.signal_time_expired_handler(signal.SIGXCPU, None)
        except Exception as exc:
            self.fail(
                "signal_time_expired_handler() raised"
                " unexpectedly: %s" % exc)


class TestServiceContainerWorkerCheckLimitsIntegration(TransactionCase):
    """Tests for check_limits() interaction with service.stop()."""

    def test_service_stop_called_when_super_sets_alive_false(self):
        worker = _make_worker()
        worker.alive = True

        fake_rusage = mock.MagicMock()
        fake_rusage.ru_utime = 0.0
        fake_rusage.ru_stime = 0.0

        def _super_kills_worker():
            worker.alive = False

        with (
            mock.patch.object(
                worker.__class__.__bases__[0],
                'check_limits',
                side_effect=_super_kills_worker),
            mock.patch('resource.getrusage', return_value=fake_rusage),
            mock.patch(
                'resource.getrlimit',
                return_value=(60, resource.RLIM_INFINITY)),
            mock.patch('resource.setrlimit'),
            mock.patch.object(worker.service, 'stop') as mock_stop,
        ):
            worker.check_limits()

        mock_stop.assert_called_once()

    def test_service_stop_not_called_when_already_dead(self):
        """If alive was False before check_limits(), don't call stop again."""
        worker = _make_worker()
        worker.alive = False  # already dead

        fake_rusage = mock.MagicMock()
        fake_rusage.ru_utime = 0.0
        fake_rusage.ru_stime = 0.0

        def _super_keeps_worker_dead():
            worker.alive = False

        with (
            mock.patch.object(
                worker.__class__.__bases__[0],
                'check_limits',
                side_effect=_super_keeps_worker_dead),
            mock.patch('resource.getrusage', return_value=fake_rusage),
            mock.patch(
                'resource.getrlimit',
                return_value=(60, resource.RLIM_INFINITY)),
            mock.patch('resource.setrlimit'),
            mock.patch.object(worker.service, 'stop') as mock_stop,
        ):
            worker.check_limits()

        mock_stop.assert_not_called()
