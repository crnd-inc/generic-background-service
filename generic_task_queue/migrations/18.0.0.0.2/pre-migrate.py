def migrate(cr, version):
    # Add runner_id column to task table (zombie-thread guard)
    cr.execute("""
        ALTER TABLE generic_task_queue_task
        ADD COLUMN IF NOT EXISTS runner_id VARCHAR
    """)

    # Add default_timeout column to task type table
    cr.execute("""
        ALTER TABLE generic_task_queue_task_type
        ADD COLUMN IF NOT EXISTS default_timeout INTEGER NOT NULL DEFAULT 0
    """)

    # Migrate any workers still in removed 'stale' state to 'dead'
    cr.execute("""
        UPDATE generic_task_queue_worker
        SET state = 'dead'
        WHERE state = 'stale'
    """)
