**Fix: `gtq_task_auto_refresh` widget crashed when `label_field` / `state_field` /
`progress_field` options were not set**

`extractProps` fell back to `null` for these options, but the OWL component
declares them as `{ type: String, optional: true }`, which accepts a missing
key or `undefined` but not `null`. Any view using the widget without every
one of these three options (the common case, since all three are optional)
failed to render with `OwlError: Invalid props for component
'TaskAutoRefreshField'`. The fallback is now `undefined`, matching the props
declaration.
