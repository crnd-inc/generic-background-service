- [IMP] `_service_shutdown_timeout` raised to 90 s so a service that is
  still draining its workers within its own `_shutdown_timeout` is not
  reported as failing to stop.
