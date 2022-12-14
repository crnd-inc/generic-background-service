Generic Background Service
==========================

.. |badge2| image:: https://img.shields.io/badge/license-LGPL--3-blue.png
    :target: http://www.gnu.org/licenses/lgpl-3.0-standalone.html
    :alt: License: LGPL-3

.. |badge5| image:: https://img.shields.io/badge/maintainer-CR&D-purple.png
    :target: https://crnd.pro/


|badge2| |badge5|


This is module that provides framework that allows to easily build
background services in Odoo. The background services are usually used
to handle asynchronous tasks, or connections to other systems
(like RabbitMQ, Kafka, etc) that require persistent connections.

This framework will automatically decide how to run the service
(as worker process or as thread), depending on the mode the Odoo is running.
Also, this framework provide ability to run service(s) as separate processes,
for example, this could be useful to run service on different machine or
to allocate separate docker (kubernetes) container for service.

In case when Odoo is started in threaded mode,
then separate thread will be allocated for each service,
and additionally separate thread will be started for
each service worker per database.

In case when Odoo is started in worker mode,
then separate worker will be started for each service,
and additionally separate thread will be started for each
worker per database.

The service creation process is pretty simple.
All you need is to create two classes:
one for service and another one for service worker.

We use following terminology:

- BackgroundService - is the service that have to be running in background.
  It is not related to any database. It is responsible on managing service workers:
  run service worker for each database, stop service worker for inactive databases,
  restart service worker on error,
  restart service worker by timeout or by other conditions (determined by worker implementation)
- ServiceWorker - is the entity that is responsible for actual work.
  The worker will be automatically started in separate thread bound to
  specific database. Also, it will be possible to access registry associated with
  that database from worker (and create Environment if needed).


Bug Tracker
===========

Bugs are tracked on `GitHub Issues <https://github.com/crnd-inc/generic-addons/issues>`_.
In case of trouble, please check there if your issue has already been reported.


Maintainer
''''''''''
.. image:: https://crnd.pro/web/image/3699/300x140/crnd.png

Our web site: https://crnd.pro/

This module is maintained by the Center of Research & Development company.

We can provide you further Odoo Support, Odoo implementation, Odoo customization, Odoo 3rd Party development and integration software, consulting services. Our main goal is to provide the best quality product for you.

For any questions `contact us <mailto:info@crnd.pro>`__.
