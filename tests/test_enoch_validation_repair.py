from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from enoch.app.core import EnochApplication, _task_worker_context
from enoch.app.validation_repair import validation_repair_attempts
from enoch.config import write_section_value
from enoch.immune import DoctorCheckResult, ImmuneResult, _run_check, diagnose_output
from enoch.identity import load_identity
from enoch.providers import AgentRuntimeAccessUnavailable, AgentRuntimeError, AuthorizationDecision
from enoch.app.epoch import begin_daemon_epoch
from our_ark_provider_kit import BranchlessRepositoryFixture, IndependentReviewFixture
from tests.test_enoch_application import _Chat, _Runtime


class ControlledRuntime(_Runtime):
    def __init__(self):
        self.calls = []
        self.action = lambda *args: 'done'

    def act_in_session(self, identity, message, *, cwd, sandbox, execution, state_root):
        self.calls.append((message, cwd, execution))
        return self.action(message, cwd, execution)


def doctor_result(*, passed=False, category='code health', detail='AssertionError: expected 2, got 1'):
    check = DoctorCheckResult('tests', passed, 'python -m unittest discover -s tests', detail,
                             category=category)
    return ImmuneResult(passed, check.command, detail, diagnose_output(detail, passed=passed), [check])


class ValidationRepairTests(unittest.TestCase):
    def setUp(self):
        for name in ('_sync_session_activity', 'ensure_long_term_memory', 'log_conversation_turn'):
            p = patch('enoch.app.core.' + name)
            p.start()
            self.addCleanup(p.stop)

    def application(self, root):
        repository = BranchlessRepositoryFixture()
        review = IndependentReviewFixture()
        runtime = ControlledRuntime()
        chat = _Chat()
        app = EnochApplication(load_identity(), root, chat, runtime=runtime,
                               repository=repository, review=review)
        def edit(message, cwd, execution):
            cwd.mkdir(parents=True, exist_ok=True)
            (cwd / 'value.txt').write_text('1')
            repository.mark_changed('value.txt')
            return 'Implemented the requested value.'
        runtime.action = edit
        return app, runtime, chat, repository, review

    def run_task(self, app):
        job = app.workflow.enqueue('room-1', 'Implement value 2', mode='direct', context='Keep the existing API.')
        app._run_direct_task_job(job, session_key='same-task-session')
        return app.workflow.find(job.id)

    def test_real_failing_test_is_repaired_then_published_once(self):
        with TemporaryDirectory() as temp:
            app, runtime, chat, repository, review = self.application(Path(temp))
            controls = []
            def edit(message, cwd, execution):
                controls.append(execution)
                cwd.mkdir(parents=True, exist_ok=True)
                if len(controls) == 1:
                    (cwd / 'tests').mkdir()
                    (cwd / 'tests/test_value.py').write_text(
                        'import unittest\nfrom pathlib import Path\n'
                        'class ValueTests(unittest.TestCase):\n'
                        '    def test_value(self):\n'
                        '        value = (Path(__file__).parents[1] / "value.txt").read_text()\n'
                        '        self.assertEqual(value, "2")\n')
                    (cwd / 'value.txt').write_text('1')
                    repository.mark_changed('value.txt', 'tests/test_value.py')
                    return 'Initial implementation.'
                self.assertFalse(review.reviews)
                self.assertIn('Validation repair 1/2', message)
                self.assertIn('AssertionError', message)
                self.assertIn('python', message)
                self.assertIn('Keep the existing API.', message)
                self.assertIn('Do not remove tests', message)
                self.assertIn('Doctor failed', app.workflow.inspect().running.result)
                (cwd / 'value.txt').write_text('2')
                (cwd / 'repair-notes.txt').write_text('Corrected the implementation.')
                repository.mark_changed('value.txt', 'tests/test_value.py', 'repair-notes.txt')
                return 'Fixed value 2.'
            runtime.action = edit
            checks = []
            def doctor(root, **kwargs):
                check = _run_check('tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], root, 20)
                checks.append(check)
                return ImmuneResult(check.passed, check.command, check.output,
                                    diagnose_output(check.output, passed=check.passed), [check])
            with patch('enoch.app.core.run_immune_system', side_effect=doctor), \
                 patch.object(repository, 'capture_change', wraps=repository.capture_change) as capture:
                result = self.run_task(app)
            self.assertEqual([check.passed for check in checks], [False, True])
            self.assertTrue(all('Ran 1 test' in check.output for check in checks))
            self.assertEqual(result.status, 'completed')
            self.assertEqual(len(review.reviews), 1)
            self.assertIn('repair-notes.txt', capture.call_args.args[0].paths)
            self.assertEqual(runtime.calls[0][1], runtime.calls[1][1])
            self.assertEqual(controls[0].session_key, controls[1].session_key)
            self.assertEqual(controls[0].started_at_monotonic, controls[1].started_at_monotonic)
            self.assertEqual(controls[0].timeout_seconds, controls[1].timeout_seconds)
            self.assertIs(controls[0].cancellation_event, controls[1].cancellation_event)
            self.assertIs(controls[0].timeout_event, controls[1].timeout_event)
            self.assertNotEqual(controls[0].request_id, controls[1].request_id)
            self.assertIn('validation-repair:1', controls[1].request_id)
            self.assertIn('Repairing validation (1/2)', '\n'.join(text for _, _, text in chat.edited))
            self.assertNotIn('Doctor failed.', result.result)
            self.assertEqual(len(app.workflow.inspect().history), 1)

    def test_repair_limit_preserves_failure_and_does_not_publish(self):
        with TemporaryDirectory() as temp:
            app, runtime, chat, repository, review = self.application(Path(temp))
            with patch('enoch.app.core.run_immune_system', return_value=doctor_result()) as doctor:
                result = self.run_task(app)
            self.assertEqual(len(runtime.calls), 3)
            self.assertEqual(doctor.call_count, 3)
            self.assertIn('Validation repair 2/2', runtime.calls[-1][0])
            self.assertEqual(result.failure_code, 'validation_failed')
            self.assertFalse(result.retryable)
            self.assertIn('after 2 automatic repair attempt(s)', result.result)
            self.assertIn('AssertionError', result.result)
            self.assertTrue(repository.workspaces)
            self.assertFalse(review.reviews)
            self.assertFalse(app.workflow.inspect().pending)

    def test_operational_failure_and_disabled_repair_stop_without_extra_model_turns(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled), TemporaryDirectory() as temp:
                root = Path(temp)
                app, runtime, chat, repository, review = self.application(root)
                if disabled:
                    write_section_value('task', 'validation_repair_attempts', '0', root)
                failure = doctor_result(category='code health' if disabled else 'operational readiness')
                with patch('enoch.app.core.run_immune_system', return_value=failure) as doctor:
                    result = self.run_task(app)
                self.assertEqual(len(runtime.calls), 1)
                self.assertEqual(doctor.call_count, 1)
                self.assertEqual(result.status, 'failed')
                self.assertFalse(review.reviews)

    def test_cancel_and_original_task_deadline_stop_before_repair(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout), TemporaryDirectory() as temp:
                app, runtime, chat, repository, review = self.application(Path(temp))
                def doctor(*args, **kwargs):
                    execution = runtime.calls[0][2]
                    (execution.timeout_event if timeout else execution.cancellation_event).set()
                    return doctor_result()
                with patch('enoch.app.core.run_immune_system', side_effect=doctor):
                    result = self.run_task(app)
                self.assertEqual(len(runtime.calls), 1)
                self.assertEqual(result.status, 'failed' if timeout else 'cancelled')
                if timeout:
                    self.assertEqual(result.failure_code, 'task_timeout')
                self.assertFalse(review.reviews)

    def test_quota_loss_during_repair_pauses_the_same_task(self):
        with TemporaryDirectory() as temp:
            app, runtime, chat, repository, review = self.application(Path(temp))
            original_action = runtime.action
            def action(*args):
                if len(runtime.calls) == 2:
                    raise AgentRuntimeAccessUnavailable('Quota unavailable.')
                return original_action(*args)
            runtime.action = action
            with patch('enoch.app.core.run_immune_system', return_value=doctor_result()) as doctor:
                result = self.run_task(app)
            self.assertEqual(result.status, 'paused')
            self.assertEqual(len(runtime.calls), 2)
            self.assertEqual(doctor.call_count, 1)
            self.assertEqual([j.id for j in app.workflow.inspect().paused], [result.id])
            self.assertTrue(repository.workspaces)
            self.assertFalse(review.reviews)

    def test_repair_rechecks_authorization_and_daemon_ownership(self):
        for takeover in (False, True):
            with self.subTest(takeover=takeover), TemporaryDirectory() as temp:
                root = Path(temp)
                app, runtime, chat, repository, review = self.application(root)
                class Policy:
                    def authorize(self, request):
                        denied = request.action == 'runtime.execute' and bool(runtime.calls)
                        return AuthorizationDecision(allowed=not denied, reason='Repair denied.' if denied else '',
                                                     denied_capabilities=('runtime.execute',) if denied else ())
                if not takeover:
                    app.effect_fence.authorizer.policy = Policy()
                def doctor(*args, **kwargs):
                    if takeover:
                        begin_daemon_epoch(root, provider='replacement')
                    return doctor_result()
                with patch('enoch.app.core.run_immune_system', side_effect=doctor):
                    result = self.run_task(app)
                self.assertEqual(len(runtime.calls), 1)
                self.assertFalse(review.reviews)
                if not takeover:
                    self.assertEqual(result.failure_code, 'authorization_denied')

    def test_erasing_all_changes_during_repair_cannot_complete_the_task(self):
        with TemporaryDirectory() as temp:
            app, runtime, chat, repository, review = self.application(Path(temp))
            original_action = runtime.action
            def action(*args):
                if len(runtime.calls) == 2:
                    repository.mark_changed()
                    return 'All changes removed.'
                return original_action(*args)
            runtime.action = action
            with patch('enoch.app.core.run_immune_system', side_effect=[doctor_result(), doctor_result(passed=True)]):
                result = self.run_task(app)
            self.assertEqual(result.failure_code, 'validation_failed')
            self.assertIn('no task changes to publish', result.result)
            self.assertFalse(review.reviews)

    def test_repair_runtime_error_keeps_doctor_evidence_and_stops(self):
        with TemporaryDirectory() as temp:
            app, runtime, chat, repository, review = self.application(Path(temp))
            original_action = runtime.action
            def action(*args):
                if len(runtime.calls) == 2:
                    raise AgentRuntimeError('Tool failed during repair.')
                return original_action(*args)
            runtime.action = action
            with patch('enoch.app.core.run_immune_system', return_value=doctor_result()):
                result = self.run_task(app)
            self.assertEqual(result.failure_code, 'validation_failed')
            self.assertIn('AssertionError', result.result)
            self.assertIn('Tool failed during repair', result.result)
            self.assertEqual(len(runtime.calls), 2)
            self.assertFalse(review.reviews)

    def test_retry_prompt_inherits_latest_failure_without_changing_original_context(self):
        with TemporaryDirectory() as temp:
            app, runtime, chat, repository, review = self.application(Path(temp))
            original = app.workflow.enqueue('room-1', 'Original requirement', context='Original context')
            app.workflow.start_next()
            app.workflow.finalize(original.id, 'failed', result='Original failing command and traceback.',
                                  failure_code='validation_failed', failure_class='permanent')
            retry = app.workflow.retry_failed(original.id)
            context = _task_worker_context(retry, workflow=app.workflow)
            self.assertIn('Original context', context)
            self.assertIn('Original failing command and traceback.', context)
            self.assertIn('validation_failed', context)
            self.assertIn(f'"previous_task_id": {original.id}', context)
            self.assertEqual(retry.text, original.text)
            self.assertEqual(retry.context, original.context)
            self.assertNotIn('Previous failed attempt', _task_worker_context(original, workflow=app.workflow))
            app.workflow.start_next()
            app.workflow.finalize(retry.id, 'failed', result='Latest failure tail.', failure_code='validation_failed')
            next_retry = app.workflow.retry_failed(retry.id)
            latest_context = _task_worker_context(next_retry, workflow=app.workflow)
            self.assertIn('Latest failure tail.', latest_context)
            self.assertNotIn('Original failing command and traceback.', latest_context)
            started = app.workflow.start_next()
            with patch('enoch.app.core.run_immune_system', return_value=doctor_result(passed=True)):
                app._run_task_job(started)
            self.assertIn('Latest failure tail.', runtime.calls[0][0])
            self.assertIn('Original requirement', runtime.calls[0][0])

    def test_invalid_repair_limits_fall_back_to_bounded_default(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for value, expected in [('0', 0), ('1', 1), ('5', 5), ('-1', 2), ('10000', 2), ('bad', 2)]:
                write_section_value('task', 'validation_repair_attempts', value, root)
                self.assertEqual(validation_repair_attempts(root), expected)
