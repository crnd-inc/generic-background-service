import logging

from odoo.addons.generic_background_service import BackgroundService

_logger = logging.getLogger(__name__)


class TaskQueueService(BackgroundService):
    _name = 'generic.task.queue.service'

    def _get_channels(self):
        return super()._get_channels() + ['background_migration']
