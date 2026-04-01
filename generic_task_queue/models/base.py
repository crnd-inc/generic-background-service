import logging

from odoo import models, api

_logger = logging.getLogger(__name__)


class Base(models.AbstractModel):
    _inherit = 'base'

    @api.model
    def _g_task_queue__plan(self, method, record_ids=None,
                            name=None, channel='default',
                            priority=5, timeout=0, **kwargs):
        """ Plan a background task to call a method on this model.

            Creates a 'task.type.model.method' task that will
            asynchronously call ``self.env[model].browse(record_ids).method(**kwargs)``.

            Usage::

                # From a recordset — uses self.ids by default
                self._g_task_queue__plan('do_heavy_work', amount=5)

                # Explicit record IDs
                self.env['res.partner']._g_task_queue__plan(
                    'compute_stats', record_ids=[1, 2, 3])

            :param str method: method name to call on the model
            :param list record_ids: record IDs (defaults to self.ids)
            :param str name: task description (auto-generated if omitted)
            :param str channel: routing channel (default: 'default')
            :param int priority: 0 = highest (default: 5)
            :param int timeout: max seconds (0 = no limit)
            :param kwargs: keyword arguments passed to the method
            :return: created task record
            :rtype: generic.task.queue.task
        """  # noqa: E501
        if record_ids is None:
            record_ids = self.ids

        # Validate that method is decorated with @background_task
        func = getattr(type(self), method, None)
        if func and not getattr(func, '_is_background_task', False):
            _logger.warning(
                "Method %s.%s is not decorated with "
                "@background_task. Task will fail at "
                "execution time.",
                self._name, method)

        if name is None:
            name = "%s.%s(%s)" % (
                self._name, method,
                ', '.join(str(i) for i in record_ids[:5]))
            if len(record_ids) > 5:
                name += '...'

        params = {
            'model': self._name,
            'method': method,
            'record_ids': list(record_ids),
        }
        if kwargs:
            params['kwargs'] = kwargs

        return self.env['generic.task.queue.task'].create_task(
            'task.type.model.method',
            name=name,
            params=params,
            channel=channel,
            priority=priority,
            timeout=timeout,
        )
