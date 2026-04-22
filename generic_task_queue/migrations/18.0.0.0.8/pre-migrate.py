def migrate(cr, version):
    cr.execute("""
        ALTER TABLE generic_task_queue_task
        ADD COLUMN IF NOT EXISTS unique_key VARCHAR
    """)
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
            generic_task_queue_task_unique_key_active_uniq
        ON generic_task_queue_task (unique_key)
        WHERE state IN (
            'pending', 'assigned', 'running', 'stuck', 'waiting'
        ) AND unique_key IS NOT NULL
    """)
