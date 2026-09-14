import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from enoch.app.core import EnochApplication, TaskContextSnapshot
from enoch.app.models import ForgeMaintenanceRequest
from enoch.app.parsing import forge_maintenance_request
from enoch.identity import load_identity
from enoch.providers import ChatEvent
from enoch.providers.forge import LocalForgeProvider
from enoch.tasks.queue import task_queue_status
from our_ark_provider_kit import BranchlessRepositoryFixture, IndependentReviewFixture
from tests.test_enoch_providers import _Chat, _Runtime


FAILED_ENOSH_REQUEST = (
    "接续任务 #3 保留的草稿，将论文监测改为实例级配置：论文列表、时间、时区和通知目的地不随软件包分发；"
    "新实例默认关闭，只有明确配置并启用后才运行。复用现有调度能力，保留基线、去重和失败报告；"
    "仅为 Gary 当前实例配置五篇论文及 America/Los_Angeles 每天18:00监测。"
    "测试不同实例互不影响，并验证下次运行时间。"
)


class ForgeMaintenanceParsingTests(unittest.TestCase):
    def test_task_four_request_is_not_a_pr_operation(self):
        self.assertIsNone(forge_maintenance_request(FAILED_ENOSH_REQUEST))

    def test_original_request_runs_as_work_with_or_without_remote_review(self):
        for remote_review in (False, True):
            with self.subTest(remote_review=remote_review), TemporaryDirectory() as temp:
                root = Path(temp)
                runtime = _Runtime()
                runtime.act_in_session = Mock(return_value='Research task handled.')
                app = EnochApplication(
                    load_identity(), root, _Chat(), runtime=runtime,
                    repository=BranchlessRepositoryFixture(), forge=LocalForgeProvider(),
                    review=IndependentReviewFixture() if remote_review else None,
                )
                with (
                    patch.object(app, '_resolve_task_context_snapshot', return_value=TaskContextSnapshot()),
                    patch.object(app, '_start_direct_work_worker') as start_worker,
                    patch.object(app.review, 'close_review') as close_review,
                    patch('enoch.app.core.log_conversation_turn'),
                    patch('enoch.app.core.ensure_long_term_memory'),
                ):
                    app.handle_event(ChatEvent(1, 'room-1', '/do ' + FAILED_ENOSH_REQUEST, 'message-1'))
                    job = start_worker.call_args.args[0]
                    app._run_direct_task_job(job, session_key=start_worker.call_args.kwargs['session_key'])
                    close_review.assert_not_called()
                runtime.act_in_session.assert_called_once()
                self.assertIn(FAILED_ENOSH_REQUEST, runtime.act_in_session.call_args.args[1])
                completed, = task_queue_status(root).history
                self.assertEqual(completed.status, 'completed')
                self.assertIn('Research task handled.', completed.result)

    def test_explicit_pr_instructions_select_only_the_named_targets(self):
        cases = [
            ('Close PR #3', (3,), None),
            ('Please close pull request #3.', (3,), None),
            ('请关闭 PR#3。', (3,), None),
            ('关掉拉取请求 #3', (3,), None),
            ('关闭合并请求 #3', (3,), None),
            ('保留 PR #1，关闭重复的 PR #2 和 #3', (2, 3), 1),
            ('Close duplicate PRs #2, #3 and PR #4; keep PR #1', (2, 3, 4), 1),
            ('Keep PR #1 and close PR #2', (2,), 1),
            ('关闭 PR #2，然后保留 PR #1', (2,), 1),
            ('Deduplicate PRs #1, #2, #3', (2, 3), 1),
            ('Dedup pull requests #1 and #2; retain PR #2', (1,), 2),
            ('去重 PR #1、#2、#3，保留 PR #2', (1, 3), 2),
            ('close PR #2 and #2; close PR #3', (2, 3), None),
        ]
        for text, close, keep in cases:
            with self.subTest(text=text):
                self.assertEqual(forge_maintenance_request(text), ForgeMaintenanceRequest(close, keep))

    def test_bare_task_issue_and_other_numbers_do_not_authorize_pr_closure(self):
        for text in (
            'Resume task #3 and disable the monitor by default',
            'Continue task #3 and close the database connection',
            '关闭任务 #3', 'Close issue #3', 'Close #3',
            '保留 #1，关闭重复的 #2 和 #3',
            'Deduplicate records #1 and #2',
            'PR #8 introduced task #3; close its connection leak',
            '接续任务 #3，参考 PR #9；新实例默认关闭',
        ):
            with self.subTest(text=text):
                self.assertIsNone(forge_maintenance_request(text))

    def test_negation_quotes_questions_and_mixed_work_are_not_shortcuts(self):
        for text in (
            'Do not close PR #3', '不要关闭 PR #3', '请勿关闭 PR #3',
            'How do I close PR #3?', 'Close PR #3?', '关闭 PR #3 吗？',
            'Fix the code that closes PR #3',
            'Implement a command: close PR #3',
            '"close PR #3"', '`close PR #3`',
            'Close PR #3 if it is a duplicate',
            'Close PR #3, then continue task #4',
            '关闭 PR #3。不要关闭 PR #4',
            'Duplicate PR #3', 'Disclose PR #3',
            'Close PR #3; keep PR #4 and task #5',
        ):
            with self.subTest(text=text):
                self.assertIsNone(forge_maintenance_request(text))

    def test_incomplete_or_conflicting_requests_do_not_close_anything(self):
        for text in (
            '', 'Close PR', 'Close PR #0 and #3', 'Close PR #-1',
            'Close PR #3 and', 'Keep PR #3', 'Dedup PR #3',
            'Dedup PR #1 and #2; keep PR #9',
            'Close PR #3; keep PR #1; keep PR #2',
            'Close PR #3; keep PR #1 and #2',
            'Close PR #3; continue',
        ):
            with self.subTest(text=text):
                self.assertIsNone(forge_maintenance_request(text))
