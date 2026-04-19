def migrate(cr, version):
    # Add propagate_progress column to task type table
    cr.execute("""
        ALTER TABLE generic_task_queue_task_type
        ADD COLUMN IF NOT EXISTS propagate_progress
            BOOLEAN NOT NULL DEFAULT false
    """)
