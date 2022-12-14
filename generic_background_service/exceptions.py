class BackgroundServiceError(Exception):
    """ Basic class for background service exceptions
    """


class ServiceConfigurationError(BackgroundServiceError):
    """ Happens when background service has incorrect configuration
    """
