Generic Task Queue (Tests)
==========================

.. |badge2| image:: https://img.shields.io/badge/license-LGPL--3-blue.png
    :target: http://www.gnu.org/licenses/lgpl-3.0-standalone.html
    :alt: License: LGPL-3

.. |badge5| image:: https://img.shields.io/badge/maintainer-CR&D-purple.png
    :target: https://crnd.pro/


|badge2| |badge5|


Technical test module for ``generic_task_queue``.

Provides:

- **Test Task Target** model (``test.task.target``) -- simple model
  with ``do_increment()`` method for testing task execution
- **Test task types** -- ``test.task.type.noop`` and ``test.task.type.echo``
  for registry and execution testing
- **"Plan Task" button** on test target records for manual UI testing
- **65 automated tests** covering task type registry, state machine,
  task claiming, worker model, auto-retry, E2E execution, and
  the ``_g_task_queue__plan()`` convenience method


Bug Tracker
===========

Bugs are tracked on `GitHub Issues <https://github.com/crnd-inc/generic-background-service/issues>`_.
In case of trouble, please check there if your issue has already been reported.


Maintainer
''''''''''
.. image:: https://crnd.pro/web/image/3699/300x140/crnd.png

Our web site: https://crnd.pro/

This module is maintained by the Center of Research & Development company.

We can provide you further Odoo Support, Odoo implementation, Odoo customization, Odoo 3rd Party development and integration software, consulting services. Our main goal is to provide the best quality product for you.

For any questions `contact us <mailto:info@crnd.pro>`__.
