def migrate(cr, version):
    # Rebuild singleton_active index to include 'stuck' state.
    # The old index only covered ('assigned', 'running'), so a stuck singleton
    # did not block new claims of the same type while its thread was alive.
    # init() will recreate it with the corrected WHERE clause.
    cr.execute("""
        DROP INDEX IF EXISTS
            generic_task_queue_task_singleton_active_idx
    """)
