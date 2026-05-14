Generic Background Migration
============================

.. |badge2| image:: https://img.shields.io/badge/license-LGPL--3-blue.png
    :target: http://www.gnu.org/licenses/lgpl-3.0-standalone.html
    :alt: License: LGPL-3

.. |badge5| image:: https://img.shields.io/badge/maintainer-CR&D-purple.png
    :target: https://crnd.pro/


|badge2| |badge5|


File-based one-shot background data migrations for Odoo, built on top of
`Generic Task Queue <https://github.com/crnd-inc/generic-background-service>`_.

Drop a ``background-<name>.py`` file in your module's
``migrations/<version>/`` directory and implement a ``migrate(env, task)``
function. The migration is discovered automatically on module install or
upgrade and executed asynchronously by the task queue worker.
