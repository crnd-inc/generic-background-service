from unittest.mock import patch

from odoo.tests.common import TransactionCase


class _MultiPhaseBase(TransactionCase):
    """Shared helpers for driving a pipeline through its lifecycle."""

    def setUp(self):
        super().setUp()
        self.Task = self.env['generic.task.queue.task']
        self.worker = self.env['generic.task.queue.worker'].create({
            'uuid': 'multi-phase-worker',
            'service_name': 'test.service',
            'state': 'active',
        })

    def _start_pipeline(self, type_code, params=None):
        """Create a pipeline parent, mark it running and run execute()."""
        from odoo.addons.generic_task_queue.service.task_type_registry \
            import TaskTypeRegistry
        parent = self.Task.create_task(
            type_code, name='Pipeline', params=params or {})
        parent = parent.sudo()
        parent.action_assign(self.worker)
        parent.action_start()
        task_type = TaskTypeRegistry().get_task_type(type_code)()
        task_type.execute(self.env, parent)
        return parent

    def _complete(self, children, outcome='done'):
        for child in children:
            child = child.sudo()
            child.action_assign(self.worker)
            child.action_start()
            if outcome == 'done':
                child.action_done(child.task_params)
            else:
                child.action_fail('boom')

    def _current_wave(self, parent):
        """Children of the wave the parent is currently waiting on."""
        ids = (parent.phase_data or {}).get('child_ids') or []
        return self.Task.browse(ids)


class TestMultiPhaseLifecycle(_MultiPhaseBase):
    """End-to-end multi-wave progression under a single root."""

    def test_first_phase_spawns_wave_and_waits(self):
        parent = self._start_pipeline(
            'test.task.type.two.phase', params={'a_count': 3})
        self.assertEqual(parent.state, 'waiting')
        self.assertEqual(parent.phase, 0)
        self.assertEqual(len(parent.child_ids), 3)
        self.assertEqual(set(parent.child_ids.mapped('state')), {'pending'})

    def test_second_phase_spawned_after_first_completes(self):
        parent = self._start_pipeline(
            'test.task.type.two.phase', params={'a_count': 2})
        wave_a = self._current_wave(parent)
        self._complete(wave_a, 'done')

        # All phase-a children done → on_all_children_done spawns phase b.
        parent._check_waiting_parent()

        self.assertEqual(parent.state, 'waiting')   # re-armed, not done
        self.assertEqual(parent.phase, 1)
        self.assertEqual(len(parent.child_ids), 4)  # 2 phase-a + 2 phase-b
        wave_b = self._current_wave(parent)
        self.assertEqual(len(wave_b), 2)
        self.assertNotEqual(set(wave_a.ids), set(wave_b.ids))
        self.assertEqual(set(wave_b.mapped('state')), {'pending'})

    def test_pipeline_completes_after_last_phase(self):
        parent = self._start_pipeline(
            'test.task.type.two.phase', params={'a_count': 2})
        self._complete(self._current_wave(parent), 'done')
        parent._check_waiting_parent()           # spawn phase b
        self._complete(self._current_wave(parent), 'done')
        parent._check_waiting_parent()           # finalize

        self.assertEqual(parent.state, 'done')
        self.assertEqual(parent.phase, 2)
        self.assertEqual(parent.task_result, {'b_children': 2})

    def test_prev_results_carries_only_previous_wave(self):
        """Phase b spawns one child per *successful* phase-a child."""
        parent = self._start_pipeline(
            'test.task.type.two.phase', params={'a_count': 3})
        wave_a = self._current_wave(parent)
        # 2 of 3 succeed, 1 cancelled (still terminal, not 'done')
        self._complete(wave_a[:2], 'done')
        wave_a[2].sudo().action_cancel()

        parent._check_waiting_parent()

        # only the 2 done phase-a children produce phase-b children
        self.assertEqual(parent.phase, 1)
        self.assertEqual(len(self._current_wave(parent)), 2)

    def test_phase_a_failure_fails_pipeline_without_phase_b(self):
        parent = self._start_pipeline(
            'test.task.type.two.phase', params={'a_count': 2})
        wave_a = self._current_wave(parent)
        self._complete(wave_a[:1], 'done')
        self._complete(wave_a[1:], 'failed')     # no_retry echo → permanent

        parent._check_waiting_parent()

        self.assertEqual(parent.state, 'failed')
        self.assertEqual(parent.phase, 0)        # never advanced
        self.assertEqual(len(parent.child_ids), 2)   # phase b never spawned

    def test_store_phase_data_survives_next_wave(self):
        """Custom phase_data keys persist when the framework records the
        next wave's child_ids (it merges, not overwrites)."""
        parent = self._start_pipeline(
            'test.task.type.two.phase', params={'a_count': 2})
        parent.store_phase_data(note='keep-me')
        self.assertEqual(parent.phase_data.get('note'), 'keep-me')

        self._complete(self._current_wave(parent), 'done')
        parent._check_waiting_parent()           # spawns phase b → merges

        self.assertEqual(parent.phase_data.get('note'), 'keep-me')
        self.assertEqual(
            set(parent.phase_data['child_ids']),
            set(self._current_wave(parent).ids))

    def test_cancel_root_cascades_across_waves(self):
        parent = self._start_pipeline(
            'test.task.type.two.phase', params={'a_count': 2})
        self._complete(self._current_wave(parent), 'done')
        parent._check_waiting_parent()           # spawn phase b (pending)

        parent.action_cancel()

        self.assertEqual(parent.state, 'cancelled')
        # live phase-b children cancelled; already-done phase-a untouched
        wave_b = self._current_wave(parent)
        self.assertEqual(set(wave_b.mapped('state')), {'cancelled'})


class TestMultiPhaseSkipAndEmpty(_MultiPhaseBase):
    """Empty phases are skipped; an all-empty pipeline finishes at once."""

    def test_empty_phase_is_skipped(self):
        parent = self._start_pipeline('test.task.type.skip.phase')
        # 'empty' phase yields nothing → framework advances to 'real'
        self.assertEqual(parent.state, 'waiting')
        self.assertEqual(parent.phase, 1)
        self.assertEqual(len(parent.child_ids), 1)

    def test_all_empty_pipeline_completes_immediately(self):
        from odoo.addons.generic_task_queue.service.task_type_registry \
            import TaskTypeRegistry
        parent = self.Task.create_task(
            'test.task.type.empty.pipeline', name='Empty').sudo()
        parent.action_assign(self.worker)
        parent.action_start()
        task_type = TaskTypeRegistry().get_task_type(
            'test.task.type.empty.pipeline')()

        task_type.execute(self.env, parent)

        # execute() spawned no children and left the task 'running';
        # the worker would now call action_done via the normal path.
        self.assertEqual(parent.state, 'running')
        self.assertFalse(parent.child_ids)


class TestMultiPhaseProgress(_MultiPhaseBase):
    """Phase-aware progress advances monotonically across phases.

    update_progress is patched (it opens a second cursor that would
    deadlock against the test transaction); we assert the value only.
    """

    def _task_type(self):
        from ..service.test_task_types import TestTaskTypeTwoPhase
        return TestTaskTypeTwoPhase()

    def test_progress_within_and_across_phases(self):
        parent = self._start_pipeline(
            'test.task.type.two.phase', params={'a_count': 2})
        task_type = self._task_type()
        wave_a = self._current_wave(parent)

        # Phase 0 (index 0), wave of 2 → 25 % then 50 %.
        expected_a = [25, 50]
        for i, child in enumerate(wave_a):
            child.sudo().action_assign(self.worker)
            child.sudo().action_start()
            child.sudo().action_done(child.task_params)
            with patch.object(
                    parent.__class__, 'update_progress') as mock_update:
                task_type.on_child_done(self.env, parent, child)
            mock_update.assert_called_once_with(expected_a[i])

        # Advance to phase b.
        parent._check_waiting_parent()
        self.assertEqual(parent.phase, 1)
        wave_b = self._current_wave(parent)

        # Phase 1 (index 1), wave of 2 → 75 % then 100 %.
        expected_b = [75, 100]
        for i, child in enumerate(wave_b):
            child.sudo().action_assign(self.worker)
            child.sudo().action_start()
            child.sudo().action_done(child.task_params)
            with patch.object(
                    parent.__class__, 'update_progress') as mock_update:
                task_type.on_child_done(self.env, parent, child)
            mock_update.assert_called_once_with(expected_b[i])

    def test_progress_skipped_when_track_progress_false(self):
        parent = self._start_pipeline(
            'test.task.type.two.phase', params={'a_count': 1})
        child = self._current_wave(parent)[0]
        child.sudo().action_assign(self.worker)
        child.sudo().action_start()
        child.sudo().action_done(child.task_params)

        task_type = self._task_type()
        task_type._track_progress = False
        with patch.object(
                parent.__class__, 'update_progress') as mock_update:
            task_type.on_child_done(self.env, parent, child)
        mock_update.assert_not_called()


class TestReArmPrimitive(_MultiPhaseBase):
    """The low-level re-arm primitive, exercised through the real
    _check_waiting_parent on a plain (non-MultiPhase) task type whose
    on_all_children_done spawns a second wave via task.spawn_children.
    """

    def _start_rearm(self):
        from odoo.addons.generic_task_queue.service.task_type_registry \
            import TaskTypeRegistry
        parent = self.Task.create_task(
            'test.task.type.rearm', name='ReArm').sudo()
        parent.action_assign(self.worker)
        parent.action_start()
        TaskTypeRegistry().get_task_type('test.task.type.rearm')().execute(
            self.env, parent)
        return parent

    def test_hook_spawns_second_wave_and_parent_stays_waiting(self):
        parent = self._start_rearm()
        self.assertEqual(parent.state, 'waiting')
        self.assertEqual(len(parent.child_ids), 1)

        # Finish wave 1 → real _check_waiting_parent runs the hook, which
        # spawns wave 2; the parent must re-arm (stay 'waiting'), not done.
        self._complete(parent.child_ids, 'done')
        parent._check_waiting_parent()

        self.assertEqual(parent.state, 'waiting')
        self.assertEqual(len(parent.child_ids), 2)
        live = parent.child_ids.filtered(lambda c: c.state == 'pending')
        self.assertEqual(len(live), 1)

    def test_second_check_completes_parent(self):
        parent = self._start_rearm()
        self._complete(parent.child_ids, 'done')
        parent._check_waiting_parent()           # spawn wave 2
        self._complete(
            parent.child_ids.filtered(lambda c: c.state == 'pending'), 'done')
        parent._check_waiting_parent()           # no new wave → complete

        self.assertEqual(parent.state, 'done')
        self.assertEqual(parent.task_result, {'finished': True})
