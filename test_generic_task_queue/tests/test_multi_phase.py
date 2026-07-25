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

    def test_phase_children_belong_to_creator_not_superuser(self):
        """Every phase's child wave must be owned by the pipeline's creator,
        even though later phases are spawned from the SUPERUSER worker poll
        context (_check_waiting_parent). Otherwise the worker would execute
        them with the wrong identity."""
        from odoo.addons.generic_task_queue.service.task_type_registry \
            import TaskTypeRegistry

        creator = self.env['res.users'].create({
            'name': 'Pipeline Op',
            'login': 'pipeline_op_phase',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.assertNotEqual(creator, self.env.ref('base.user_root'))

        # Created by a normal user — like a real enqueue from a UI action.
        parent = self.Task.with_user(creator).create_task(
            'test.task.type.two.phase', name='Pipeline', params={'a_count': 1})
        self.assertEqual(parent.create_uid, creator)

        parent = parent.sudo()
        parent.action_assign(self.worker)
        parent.action_start()
        task_type = TaskTypeRegistry().get_task_type(
            'test.task.type.two.phase')()
        # The worker runs execute() as the creating user.
        task_type.execute(self.env(user=creator.id), parent)

        wave_a = self._current_wave(parent)
        self.assertEqual(wave_a.create_uid, creator)

        self._complete(wave_a, 'done')
        # Advance phases the way the worker does — from a SUPERUSER context.
        parent.sudo()._check_waiting_parent()

        wave_b = self._current_wave(parent)
        self.assertTrue(wave_b)
        self.assertEqual(wave_b.create_uid, creator)

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


class TestExecutionIdentity(_MultiPhaseBase):
    """execute() and the lifecycle hooks must run as the task's creator
    (least privilege), while protected-field framework writes still succeed
    via explicit sudo().
    """

    def setUp(self):
        super().setUp()
        from ..service.test_task_types import TestTaskTypeIdentityProbe
        self.Probe = TestTaskTypeIdentityProbe
        self.Probe.seen.clear()
        self.creator = self.env['res.users'].create({
            'name': 'Pipeline Op',
            'login': 'pipeline_op_identity',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })

    def _bind(self, task):
        """Mimic the worker: user-scoped env + user-bound task record."""
        user_env = self.env(user=self.creator.id)
        return user_env, task.with_env(user_env)

    def _start_as_creator(self):
        parent = self.Task.with_user(self.creator).create_task(
            'test.task.type.identity.probe', name='Probe')
        parent.sudo().action_assign(self.worker)
        parent.sudo().action_start()
        user_env, user_task = self._bind(parent)
        self.Probe().execute(user_env, user_task)
        return parent

    def test_execute_runs_as_creator_not_superuser(self):
        self._start_as_creator()
        self.assertEqual(self.Probe.seen['execute'], (self.creator.id, False))

    def test_execute_framework_writes_succeed_under_creator(self):
        """_start_wave's phase/phase_data writes + action_wait_children must
        succeed even though execute ran as a non-super user."""
        parent = self._start_as_creator().sudo()
        self.assertEqual(parent.state, 'waiting')
        self.assertEqual(parent.phase, 0)
        self.assertTrue((parent.phase_data or {}).get('child_ids'))

    def test_on_all_children_done_runs_as_creator(self):
        parent = self._start_as_creator()
        self._complete(self._current_wave(parent.sudo()), 'done')
        # Real worker poll path — binds the creator internally.
        parent.sudo()._check_waiting_parent()
        self.assertEqual(
            self.Probe.seen['on_all_children_done'], (self.creator.id, False))
        self.assertEqual(parent.sudo().state, 'done')

    def test_protected_writes_use_explicit_sudo(self):
        """action_wait_children / store_phase_data write protected fields and
        must work when called on a task bound to the (non-super) creator."""
        parent = self.Task.with_user(self.creator).create_task(
            'test.task.type.noop', name='FW')
        parent.sudo().action_assign(self.worker)
        parent.sudo().action_start()

        user_task = parent.with_user(self.creator)
        self.assertFalse(user_task.env.su)

        user_task.action_wait_children()              # protected 'state'
        self.assertEqual(parent.sudo().state, 'waiting')

        user_task.store_phase_data(note='kept')        # protected 'phase_data'
        self.assertEqual((parent.sudo().phase_data or {}).get('note'), 'kept')


class TestHeterogeneousWave(_MultiPhaseBase):
    """A single phase may fan out children of more than one task type."""

    def test_wave_spawns_mixed_types(self):
        parent = self._start_pipeline('test.task.type.mixed.wave')
        wave = self._current_wave(parent)
        self.assertEqual(len(wave), 3)
        self.assertEqual(
            sorted(wave.mapped('type_code')),
            ['test.task.type.echo', 'test.task.type.echo',
             'test.task.type.noop'])

    def test_per_child_field_overrides_applied(self):
        parent = self._start_pipeline('test.task.type.mixed.wave')
        wave = self._current_wave(parent)
        notify = wave.filtered(
            lambda c: c.type_code == 'test.task.type.noop')
        chunks = wave.filtered(
            lambda c: c.type_code == 'test.task.type.echo')
        # dict spec overrode channel + priority for the notify child only
        self.assertEqual(notify.channel, 'fast')
        self.assertEqual(notify.priority, 1)
        # tuple-form children inherit the parent's channel
        self.assertEqual(set(chunks.mapped('channel')), {parent.channel})

    def test_mixed_wave_naming_is_global(self):
        parent = self._start_pipeline('test.task.type.mixed.wave')
        wave = self._current_wave(parent)
        # names are numbered [i/total] across the whole heterogeneous wave
        self.assertEqual(
            sorted(wave.mapped('name')),
            ['%s [1/3]' % parent.name,
             '%s [2/3]' % parent.name,
             '%s [3/3]' % parent.name])

    def test_mixed_wave_completes_and_prev_results_span_all_types(self):
        parent = self._start_pipeline('test.task.type.mixed.wave')
        self._complete(self._current_wave(parent), 'done')
        parent._check_waiting_parent()

        self.assertEqual(parent.state, 'done')
        self.assertEqual(
            parent.task_result,
            {'types': ['test.task.type.echo', 'test.task.type.noop']})


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
