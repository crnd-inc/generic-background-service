def background_task(func):
    """ Decorator that marks a model method as callable
        from the task queue via ModelMethodTaskType.

        Only methods decorated with @background_task can be
        invoked as background tasks. This prevents arbitrary
        code execution via task parameters.

        Usage::

            from odoo.addons.generic_task_queue.tools.decorators import (
                background_task,
            )

            class MyModel(models.Model):
                _name = 'my.model'

                @background_task
                def do_heavy_work(self, amount=1):
                    for record in self:
                        record.value += amount
    """
    func._is_background_task = True
    return func
