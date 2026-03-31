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

        Registration is locked after first initialization
        (when the singleton is created). Task types defined after
        that point are silently ignored with a warning.
    """
    _registered_types: Dict[str, List[Type]] = collections.defaultdict(list)

    # Initialized (merged) task type classes: {name: cls}
    _initialized_types: Dict[str, Type] = {}

    # Registration is locked after initialize()
    _registration_allowed = True

    # Singleton instance
    _registry_instance = None

    @classmethod
    def register_type(cls, name, type_cls):
        """ Register a task type class for the given name.

            :param str name: dotted task type name
            :param type type_cls: class that defines (or extends)
                the task type
        """
        if not cls._registration_allowed:
            _logger.warning(
                "Registration of task types is not allowed at the moment. "
                "May be you have to add module that defines task type "
                "'%s' to server_wide_modules config param.", name)
            return
        cls._registered_types[name].append(type_cls)

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
        cls._registration_allowed = False
        for type_name, type_defs in cls._registered_types.items():
            type_cls = type(type_name, tuple(type_defs), {})
            cls._initialized_types[type_name] = type_cls

    @classmethod
    def get_initialized_types(cls) -> Dict[str, Type]:
        """ Return dict of all initialized task types.
        """
        return cls._initialized_types

    @classmethod
    def get_task_type(cls, type_code):
        """ Return the task type class for the given type code.

            :param str type_code: dotted task type name
            :return: task type class
            :raises KeyError: if type_code is not registered
        """
        return cls._initialized_types[type_code]

    def __new__(cls, *args, **kwargs):
        if cls._registry_instance is None:
            cls.initialize()
            cls._registry_instance = super().__new__(cls, *args, **kwargs)
        return cls._registry_instance
