**Migrations run on the default task-queue worker — no service override**

Background migration tasks now use the `default` channel and are handled by
the stock task-queue worker out of the box. The module no longer overrides
`generic.task.queue.service` to add a `background_migration` channel, so a
standard installation needs no extra service wiring. In-flight migration tasks
on the old channel are re-pointed to `default` on upgrade.
