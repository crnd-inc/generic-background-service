from .task_type import AbstractTaskType


class ModelMethodTaskType(AbstractTaskType):
    """ Default task type that calls an Odoo model method.

        Task params (JSON)::

            {
                "model": "res.partner",
                "method": "compute_something",
                "record_ids": [1, 2, 3],
                "kwargs": {"force": true}
            }

        Executes: ``env[model].browse(record_ids).method(**kwargs)``

        This is the simplest way to run any existing model method
        as a background task without writing a custom task type.
    """
    _name = 'task.type.model.method'

    # Methods that must not be callable via this task type
    _forbidden_methods = frozenset({
        'unlink', 'write', 'create',
        '__delattr__', '__setattr__',
    })

    def execute(self, env, task):
        params = task.task_params
        model_name = params['model']
        method_name = params['method']
        record_ids = params.get('record_ids', [])
        kwargs = params.get('kwargs', {})

        if method_name.startswith('_'):
            raise ValueError(
                "Calling private methods is not allowed: %s" % method_name)
        if method_name in self._forbidden_methods:
            raise ValueError(
                "Calling method %s is not allowed" % method_name)
        if model_name not in env:
            raise ValueError(
                "Model %s is not available" % model_name)

        records = env[model_name].browse(record_ids)
        method = getattr(records, method_name)
        result = method(**kwargs)
        return result
