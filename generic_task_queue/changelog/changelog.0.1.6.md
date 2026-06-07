**Explicit spawn/create API for task batches**

`create_children`, `spawn_children` and `create_tasks` now take `TaskSpec`
values (and/or `TaskListSpec` builders) **positionally**, with a keyword-only
`type_code` + `params_list` homogeneous shorthand. This replaces the previous
overloaded second argument.

**Upgrade note:** the positional homogeneous form
`create_children(parent, 'my.type', [params])` is no longer accepted — use
`create_children(parent, type_code='my.type', params_list=[params])` (or pass
`TaskSpec`s positionally). Such calls now raise a clear error.
