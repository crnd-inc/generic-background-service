from odoo.tests.common import TransactionCase

from odoo.addons.generic_background_service.service.background_service import (
    BackgroundService,
)
from odoo.addons.generic_background_service.service.background_service_container import (  # noqa: E501
    wrap_service_as_thread,
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
