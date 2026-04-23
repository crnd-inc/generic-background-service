def migrate(cr, version):
    cr.execute("""
        ALTER TABLE generic_task_queue_task_type
        ADD COLUMN IF NOT EXISTS service_name VARCHAR NOT NULL
               DEFAULT ''
    """)
    cr.execute("""
        ALTER TABLE generic_task_queue_task_type
        ADD COLUMN IF NOT EXISTS default_channel VARCHAR NOT NULL
               DEFAULT 'default'
    """)
