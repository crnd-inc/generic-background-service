**TaskSpec / TaskListSpec for describing tasks**

Heterogeneous child waves and top-level batches are now described with the
`TaskSpec` value object and the `TaskListSpec` builder (in
`generic_task_queue.tools`) instead of raw tuples/dicts. `create_children` takes
either a `type_code` + params list (homogeneous) or an iterable of `TaskSpec`
(heterogeneous); the new `create_tasks(batch)` enqueues a top-level batch.
