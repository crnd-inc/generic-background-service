from .task_type import AbstractTaskType


class ModelMethodTaskType(AbstractTaskType):
    """ Default task type that calls an Odoo model method.

        Only methods decorated with ``@background_task`` are callable.

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

    def execute(self, env, task):
        params = task.task_params
        model_name = params['model']
        method_name = params['method']
        record_ids = params.get('record_ids', [])
        kwargs = params.get('kwargs', {})

        if method_name.startswith('_'):
            raise ValueError(
                "Calling private methods is not allowed: %s"
                % method_name)
        if model_name not in env:
            raise ValueError(
                "Model %s is not available" % model_name)

        records = env[model_name].browse(record_ids)

        # Validate that all record IDs exist
        if record_ids:
            existing = records.exists()
            if len(existing) != len(record_ids):
                missing = set(record_ids) - set(existing.ids)
                raise ValueError(
                    "Records not found in %s: %s"
                    % (model_name, missing))

        method = getattr(records, method_name)

        if not getattr(method, '_is_background_task', False):
            raise ValueError(
                "Method %s.%s is not decorated with "
                "@background_task" % (model_name, method_name))

        result = method(**kwargs)
        return result
