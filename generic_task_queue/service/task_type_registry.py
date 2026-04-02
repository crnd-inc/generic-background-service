import collections
import logging

from typing import Dict, List, Type

_logger = logging.getLogger(__name__)


class TaskTypeRegistry:
    """ Singleton registry for task types.

        Task types are registered automatically via
        AbstractTaskType.__init_subclass__ when a subclass
        defines a non-None _name attribute.

        Supports extension: multiple classes with the same _name
        are merged into a single class (MRO-based inheritance).

        Registration is always open — task types can be registered
        at any time (including after module loading). New types
        are built lazily on first access via get_task_type().
    """
    _registered_types: Dict[str, List[Type]] = collections.defaultdict(list)

    # Initialized (merged) task type classes: {name: cls}
    _initialized_types: Dict[str, Type] = {}

    # Singleton instance
    _registry_instance = None

    @classmethod
    def register_type(cls, name, type_cls):
        """ Register a task type class for the given name.

            :param str name: dotted task type name
            :param type type_cls: class that defines (or extends)
                the task type
        """
        cls._registered_types[name].append(type_cls)
        # Invalidate initialized cache so it's rebuilt on next access
        cls._initialized_types.pop(name, None)

    @classmethod
    def initialize(cls):
        """ Merge all registered definitions for each task type name
            into a single class (via multiple inheritance).

            For example, if two classes define _name = 'my.task.type':

                class MyTaskType(AbstractTaskType):
                    _name = 'my.task.type'

                class MyTaskTypeExtension(AbstractTaskType):
                    _name = 'my.task.type'

            This method creates:

                type('my.task.type', (MyTaskType, MyTaskTypeExtension), {})
        """
        for type_name, type_defs in cls._registered_types.items():
            if type_name not in cls._initialized_types:
                type_cls = type(type_name, tuple(type_defs), {})
                cls._initialized_types[type_name] = type_cls

    @classmethod
    def get_initialized_types(cls) -> Dict[str, Type]:
        """ Return dict of all initialized task types.
        """
        # Ensure all registered types are initialized
        cls.initialize()
        return cls._initialized_types

    @classmethod
    def get_task_type(cls, type_code):
        """ Return the task type class for the given type code.

            Lazy-builds the merged class on first access if the
            type was registered after initial initialization.

            :param str type_code: dotted task type name
            :return: task type class
            :raises KeyError: if type_code is not registered
        """
        if type_code not in cls._initialized_types:
            if type_code in cls._registered_types:
                # Late registration — build now
                defs = cls._registered_types[type_code]
                cls._initialized_types[type_code] = type(
                    type_code, tuple(defs), {})
            else:
                raise KeyError(type_code)
        return cls._initialized_types[type_code]

    def __new__(cls, *args, **kwargs):
        if cls._registry_instance is None:
            cls.initialize()
            cls._registry_instance = super().__new__(cls, *args, **kwargs)
        return cls._registry_instance
