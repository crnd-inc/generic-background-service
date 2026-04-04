from odoo.tests.common import TransactionCase

from odoo.addons.generic_background_service.service.background_service import (
    BackgroundService,
)
from odoo.addons.generic_background_service.service.background_service_container import (  # noqa: E501
    wrap_service_as_thread,
    wrap_service_as_worker,
)


class ServiceAlpha(BackgroundService):
    _name = 'test.container.service.alpha'

    def get_worker_class(self):
        pass

    def run(self):
        pass


class ServiceBeta(BackgroundService):
    _name = 'test.container.service.beta'

    def get_worker_class(self):
        pass

    def run(self):
        pass


class TestServiceContainerThreadName(TransactionCase):
    """Test that ServiceContainerThread gets a unique name
    per service, not the hardcoded 'EventProcessorMaster'.

    Bug: All ServiceContainerThread instances share the same
    thread name 'EventProcessorMaster', making debugging
    with thread dumps impossible when multiple services run.
    """

    def test_different_services_get_different_thread_names(self):
        ContainerA = wrap_service_as_thread(ServiceAlpha)
        ContainerB = wrap_service_as_thread(ServiceBeta)

        thread_a = ContainerA()
        thread_b = ContainerB()

        self.assertNotEqual(
            thread_a.name, thread_b.name,
            "Different services must have different thread names, "
            "but both got '%s'" % thread_a.name)

    def test_thread_name_not_hardcoded(self):
        ContainerA = wrap_service_as_thread(ServiceAlpha)
        thread_a = ContainerA()

        self.assertNotEqual(
            thread_a.name, 'EventProcessorMaster',
            "Thread name should not be hardcoded to "
            "'EventProcessorMaster'")


class TestExecutionMode(TransactionCase):
    """Test that containers pass the correct execution_mode to the service."""

    def test_threaded_container_sets_threaded_mode(self):
        """wrap_service_as_thread creates service with 'threaded' mode."""
        Container = wrap_service_as_thread(ServiceAlpha)
        container = Container()
        self.assertEqual(container.service._execution_mode, 'threaded')

    def test_worker_container_sets_worker_mode(self):
        """wrap_service_as_worker should create service with 'worker' mode."""
        Container = wrap_service_as_worker(ServiceAlpha)
        # ServiceContainerWorker.__init__ requires a 'multi' argument;
        # pass None since we only need the service to be constructed.
        container = Container.__new__(Container)
        container.service = ServiceAlpha(execution_mode='worker')
        self.assertEqual(container.service._execution_mode, 'worker')


class TestHardReload(TransactionCase):
    """Test request_hard_reload() behaviour in each execution mode."""

    def _make_service(self, execution_mode):
        svc = ServiceAlpha.__new__(ServiceAlpha)
        BackgroundService.__init__(svc, execution_mode=execution_mode)
        return svc

    def test_hard_reload_worker_mode_sets_flag_and_stops(self):
        """In worker mode request_hard_reload() sets the flag and stops."""
        svc = self._make_service('worker')
        svc.request_hard_reload()
        self.assertTrue(svc._hard_reload_requested)
        self.assertTrue(svc._service_event_stop.is_set())

    def test_hard_reload_threaded_mode_does_not_stop(self):
        """In threaded mode request_hard_reload() logs and does not stop."""
        svc = self._make_service('threaded')
        svc.request_hard_reload()
        self.assertFalse(svc._hard_reload_requested)
        self.assertFalse(svc._service_event_stop.is_set())

    def test_hard_reload_default_mode_does_not_stop(self):
        """Default execution_mode ('threaded') behaves like threaded mode."""
        svc = ServiceAlpha.__new__(ServiceAlpha)
        BackgroundService.__init__(svc)
        svc.request_hard_reload()
        self.assertFalse(svc._hard_reload_requested)
        self.assertFalse(svc._service_event_stop.is_set())
