**Multi-phase task pipelines**

A task can now run several sequential waves of child tasks under one root
(`MultiPhaseTaskType` with `_phases` / `plan_phase`), with phase-aware progress.

**Background tasks now run as their creator, not as the superuser**

Task `execute()` and lifecycle hooks now run with the creating user's access
rights (least-privilege). Task types needing elevated access must call `.sudo()`
explicitly.
