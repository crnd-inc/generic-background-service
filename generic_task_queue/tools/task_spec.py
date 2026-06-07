""" Value objects for describing tasks to create.

    ``TaskSpec`` is the canonical, validated description of a single task.
    ``TaskListSpec`` is a builder for a heterogeneous batch of them (built
    conditionally, possibly with mixed types). Both are used to spawn child
    waves (``create_children`` / ``spawn_children``) and to enqueue top-level
    batches (``create_tasks``).

    These are pure Python — no Odoo imports — so they can be constructed and
    unit-tested anywhere and serialized through a single ``to_vals()`` path.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime

_logger = logging.getLogger(__name__)

# Deprecated retry_policy aliases → canonical names. Single source of truth,
# shared by TaskSpec.to_vals() and the task model's create_task().
_RETRY_POLICY_ALIASES = {
    'non_retriable': 'no_retry',
    'retriable':     'retry_any',
}


def normalize_retry_policy(value):
    """ Translate a deprecated retry_policy alias to its canonical name.

        Warns so callers know to update; returns the value unchanged when it
        is not a known alias.
    """
    canonical = _RETRY_POLICY_ALIASES.get(value)
    if canonical is not None:
        _logger.warning(
            "Deprecated retry_policy value %r — use %r instead. "
            "Old names will be removed in a future release.",
            value, canonical)
        return canonical
    return value


@dataclass(frozen=True)
class TaskSpec:
    """ Immutable description of one task to create.

        ``type_code`` and ``params`` are the task type and its JSON input.
        The remaining fields are optional per-task overrides; left as ``None``
        they fall back to the parent's channel (for children) or the task
        type's defaults (for top-level tasks). Only this whitelisted set of
        fields may be set — arbitrary task fields cannot be injected.

        Example::

            TaskSpec('my.upload.chunk', {'ids': [1, 2]})
            TaskSpec('my.notify', {'message': 'done'},
                     channel='fast', priority=0)
    """
    type_code: str
    params: dict | None = None
    name: str | None = None
    channel: str | None = None
    priority: int | None = None
    eta: datetime | None = None
    timeout: int | None = None
    retry_policy: str | None = None
    max_retries: int | None = None

    # Fields written verbatim as task vals (besides type_code / task_params).
    # Not annotated → not a dataclass field.
    _OVERRIDE_FIELDS = (
        'name', 'channel', 'priority', 'eta', 'timeout',
        'retry_policy', 'max_retries',
    )

    @classmethod
    def build(cls, type_code, params=None, **overrides):
        """ Construct a TaskSpec, validating override field names.

            Same as ``TaskSpec(type_code, params, **overrides)`` but raises a
            clear error naming the offending key(s) and the valid overrides,
            instead of a raw ``TypeError`` from the dataclass constructor.
            Used wherever overrides come from caller-supplied keyword args
            (``TaskListSpec.add`` / ``create_children`` common_vals).
        """
        unknown = set(overrides) - set(cls._OVERRIDE_FIELDS)
        if unknown:
            raise ValueError(
                "Unknown TaskSpec field(s): %s. Valid overrides: %s." % (
                    ', '.join(sorted(unknown)),
                    ', '.join(cls._OVERRIDE_FIELDS)))
        return cls(type_code, params, **overrides)

    def __post_init__(self):
        if not isinstance(self.type_code, str) or not self.type_code:
            raise ValueError(
                "TaskSpec.type_code must be a non-empty string, got %r"
                % (self.type_code,))
        if self.params is not None and not isinstance(self.params, dict):
            raise ValueError(
                "TaskSpec.params must be a dict or None, got %s"
                % (type(self.params).__name__,))

    def to_vals(self) -> dict:
        """ Return the ``create()`` vals this spec specifies.

            Includes ``type_code`` and ``task_params`` always, plus each
            override field that is set (not ``None``). No parent context —
            the caller adds ``parent_id`` / default name / default channel.
        """
        vals = {
            'type_code': self.type_code,
            'task_params': self.params or {},
        }
        for field in self._OVERRIDE_FIELDS:
            value = getattr(self, field)
            if value is not None:
                vals[field] = value
        if 'retry_policy' in vals:
            vals['retry_policy'] = normalize_retry_policy(vals['retry_policy'])
        return vals

    def replace(self, **changes) -> 'TaskSpec':
        """ Return a copy of this spec with the given fields changed. """
        return dataclasses.replace(self, **changes)


class TaskListSpec:
    """ Builder for a heterogeneous batch of :class:`TaskSpec`.

        Accumulates specs (often conditionally, with mixed task types) and is
        iterable, so it can be passed anywhere an iterable of ``TaskSpec`` is
        accepted (``create_children`` / ``spawn_children`` / ``create_tasks``).

        Batch-level defaults passed to the constructor are applied to every
        added spec; a per-spec override wins over a default.

        Example::

            batch = (TaskListSpec(channel='heavy', retry_policy='retry_known')
                     .add_many('my.upload.chunk', chunk_params)
                     .add('my.notify', {'message': 'done'}, channel='fast'))
            parent.spawn_children(batch)
    """

    def __init__(self, **defaults):
        self._defaults = dict(defaults)
        self._specs: list[TaskSpec] = []

    def add(self, type_code, params=None, **overrides) -> 'TaskListSpec':
        """ Append a task; ``overrides`` win over the batch defaults. """
        merged = {**self._defaults, **overrides}
        self._specs.append(TaskSpec.build(type_code, params, **merged))
        return self

    def add_spec(self, spec: TaskSpec) -> 'TaskListSpec':
        """ Append an already-built :class:`TaskSpec` (defaults not applied).
        """
        if not isinstance(spec, TaskSpec):
            raise TypeError(
                "add_spec() expects a TaskSpec, got %s"
                % (type(spec).__name__,))
        self._specs.append(spec)
        return self

    def add_many(self, type_code, params_list,
                 **overrides) -> 'TaskListSpec':
        """ Append one task per params dict (homogeneous sub-batch). """
        for params in params_list:
            self.add(type_code, params, **overrides)
        return self

    def extend(self, specs) -> 'TaskListSpec':
        """ Append an iterable of :class:`TaskSpec`. """
        for spec in specs:
            self.add_spec(spec)
        return self

    def __iter__(self):
        return iter(self._specs)

    def __len__(self):
        return len(self._specs)

    def __bool__(self):
        return bool(self._specs)
