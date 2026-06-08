def migrate(cr, version):
    """Re-channel active migration tasks from the now-removed
    'background_migration' channel to 'default'.

    Earlier versions routed migration tasks to a dedicated
    'background_migration' channel and extended the default service to
    listen on it. That extension is gone — migration tasks now run on the
    'default' channel handled by the stock worker. Any in-flight task
    created on the old channel would otherwise never be claimed.
    """
    cr.execute("""
        UPDATE generic_task_queue_task
           SET channel = 'default'
         WHERE channel = 'background_migration'
           AND state IN ('pending', 'assigned', 'running', 'stuck', 'waiting')
    """)
