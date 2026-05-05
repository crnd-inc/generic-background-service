def migrate(cr, version):
    # Rename retry policy values to the new three-policy scheme.
    # 'retriable'    → 'retry_any'  (retry on any exception)
    # 'non_retriable'→ 'no_retry'   (never auto-retry)
    # New value 'retry_known' is not present in old data.
    cr.execute("""
        UPDATE generic_task_queue_task
        SET retry_policy = CASE
            WHEN retry_policy = 'retriable'     THEN 'retry_any'
            WHEN retry_policy = 'non_retriable' THEN 'no_retry'
            ELSE retry_policy
        END
        WHERE retry_policy IN ('retriable', 'non_retriable')
    """)
    cr.execute("""
        UPDATE generic_task_queue_task_type
        SET default_retry_policy = CASE
            WHEN default_retry_policy = 'retriable'     THEN 'retry_any'
            WHEN default_retry_policy = 'non_retriable' THEN 'no_retry'
            ELSE default_retry_policy
        END
        WHERE default_retry_policy IN ('retriable', 'non_retriable')
    """)
