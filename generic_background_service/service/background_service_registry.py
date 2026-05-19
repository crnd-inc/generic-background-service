import collections
import logging

from typing import Dict, List, Type

_logger = logging.getLogger(__name__)


# TODO: split registry on two: for workers and for threads
class BackgroundServiceRegistry:
    """ Singleton class, that represents registry for background services.
    """
    _registered_services: Dict[str, List[Type]] = collections.defaultdict(list)

    # Dict for initialized services. See method 'initialize(cls)`
    # Format: {service_name: service_cls}
    _initialized_services: Dict[str, Type] = {}

    # When all the services already started, no registration of services
    # allowed.
    # If new service (or service extension) tries to register when registration
    # is not allowed (when all background services started),
    # then it will be ignored.
    # This parameter will be set to False, when all background services
    # started.
    _registration_allowed = True

    # Reference to instance of registry (because registry is singleton)
    _registry_instance = None

    @classmethod
    def register_service(cls, name, service_cls):
        """ Register the service cls for service name in registry.

            :param str name: name of service to register definition for
            :param type service_cls: Class that represents
                 definition of service

            **MRO ordering**: classes are inserted at position 0, so the
            *last*-registered class ends up first in the list and therefore
            first (highest priority) in the MRO of the composed class
            created by ``initialize()``.  Because Odoo loads modules in
            dependency order (dependencies first), an extension module is
            always loaded *after* the module it depends on, which means
            its definition is registered last and correctly takes
            precedence via ``super()``.  This mirrors the Odoo ORM's
            own "last ``_inherit`` wins" convention.
        """
        if not cls._registration_allowed:
            _logger.warning(
                "Registration of services is not allowed at the moment."
                "May be you have to add module that defines service '%s' to"
                "system_wide_modules config param.", name)
            return
        cls._registered_services[name].insert(0, service_cls)

    @classmethod
    def initialize(cls):
        """ Initialized the service classes.
            This method, will gather all definitions of all service, and them
            it will create new class for each service, that will inherit
            from all definitions of the service.

            For example, if we define service with two definitions:

                class MyServiceDefinition(BackgroundService):
                    _name = 'my.service'

                class MyServiceExtension(BackgroundService):
                    _name = 'my.service'

            This method will create new class for this service:

                class MyService(MyServiceExtension, MyServiceDefinition)

            The resulting class will be stored in _initialized_services attr
        """
        cls._registration_allowed = False
        for service_name, service_defs in cls._registered_services.items():
            service_cls = type(service_name, tuple(service_defs), {})
            cls._initialized_services[service_name] = service_cls

    @classmethod
    def get_initialized_services(cls) -> Dict[str, Type]:
        """ Return dict with initialized service
        """
        return cls._initialized_services

    @classmethod
    def get_service_class(cls, service_name):
        """ Return class to be used for service specified by service name
        """
        return cls.get_initialized_services()[service_name]

    # Implement new to make this registry *Singleton*
    def __new__(cls, *args, **kwargs):
        if cls._registry_instance is None:
            # Initialize services
            cls.initialize()
            # Create singleton instance
            cls._registry_instance = super().__new__(cls, *args, **kwargs)
        return cls._registry_instance
