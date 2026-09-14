from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from enoch.app.core import EnochApplication
from enoch.config import write_section_value
from enoch.identity import load_identity
from enoch.tasks.queue import TaskRetryError
from our_ark_provider_kit import BranchlessRepositoryFixture, IndependentReviewFixture, RepositoryRevision
from tests.test_enoch_application import _Chat
from tests.test_enoch_validation_repair import ControlledRuntime, doctor_result


class UnpublishedReview(IndependentReviewFixture):
    supports_remote_review = False

    def publish_review(self, request, root=None):
        review = super().publish_review(request, root)
        return replace(review, state="unpublished", identity=replace(
            review.identity, id=f"legacy-review:{request.revision.id}", url="",
        ))


class WorkspaceRepository(BranchlessRepositoryFixture):
    def create_repository_workspace(self, request, root=None):
        workspace = super().create_repository_workspace(request, root)
        self.current = workspace.current_revision
        return workspace

    def capture_change(self, request, root=None):
        result = super().capture_change(request, root)
        for key, workspace in self.workspaces.items():
            if workspace.path == root:
                self.workspaces[key] = replace(workspace, current_revision=result.revision)
        return result


class PublicationTests(unittest.TestCase):
    def setUp(self):
        for name in ("_sync_session_activity", "ensure_long_term_memory", "log_conversation_turn"):
            p = patch("enoch.app.core." + name)
            p.start()
            self.addCleanup(p.stop)
        p = patch("enoch.app.core.run_immune_system", return_value=doctor_result(passed=True))
        self.doctor = p.start()
        self.addCleanup(p.stop)
        p = patch.object(EnochApplication, "_maybe_start_task_worker")
        p.start()
        self.addCleanup(p.stop)
        p = patch("enoch.app.core.automatic_retry_delay_seconds", return_value=0)
        p.start()
        self.addCleanup(p.stop)

    def application(self, root, *, required=True, review=None):
        repository = WorkspaceRepository()
        runtime = ControlledRuntime()
        if required:
            write_section_value("task", "require_remote_review", "true", root)
        app = EnochApplication(load_identity(), root, _Chat(), runtime=runtime,
                               repository=repository, review=review or UnpublishedReview())
        def edit(message, cwd, execution):
            cwd.mkdir(parents=True, exist_ok=True)
            (cwd / "value.txt").write_text("saved work")
            repository.mark_changed("value.txt")
            return "Implemented."
        runtime.action = edit
        return app, repository, runtime

    def run_task(self, app, text="Implement and create a PR"):
        job = app.workflow.enqueue("room-1", text, mode="direct")
        app._run_direct_task_job(job, session_key="publication-test")
        return app.workflow.find(job.id)

    def run_retry(self, app, job):
        running = app.workflow.start_next()
        self.assertIsNotNone(running)
        self.assertEqual(running.id, job.id)
        app._run_task_job(running)

    def test_required_remote_review_preserves_capture_then_retry_only_publishes(self):
        with TemporaryDirectory() as temp:
            app, repository, runtime = self.application(Path(temp))
            original = self.run_task(app)
            self.assertEqual(original.status, "failed")
            self.assertEqual(original.failure_code, "review_provider_required")
            self.assertEqual(original.publish_stage, "captured")
            self.assertFalse(original.review_published)
            self.assertFalse(original.retryable)  # Configuration needs intervention.
            self.assertEqual(len(runtime.calls), 1)
            self.assertTrue(repository.workspaces)
            self.assertEqual((Path(original.workspace_path) / "value.txt").read_text(), "saved work")
            revisions = len(repository.revisions)
            app.review = IndependentReviewFixture()
            retried = app.workflow.retry_failed(original.id)
            self.run_retry(app, retried)
            completed = app.workflow.find(retried.id)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.revision_id, original.revision_id)
            self.assertEqual(completed.publish_stage, "review_published")
            self.assertTrue(completed.review_published)
            self.assertTrue(completed.review_url)
            self.assertEqual(len(repository.revisions), revisions)
            self.assertEqual(len(runtime.calls), 1)
            self.assertEqual(self.doctor.call_count, 1)
            self.assertFalse(repository.workspaces)
            self.assertEqual(app.workflow.find(original.id), original)

    def test_local_capture_does_not_claim_publication_and_can_be_published_later(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            app, repository, runtime = self.application(root, required=False)
            original = self.run_task(app, "Implement locally")
            self.assertEqual(original.status, "completed")
            self.assertEqual(original.publish_stage, "captured")
            self.assertFalse(original.review_published)
            self.assertFalse(repository.workspaces)
            # Model the pre-fix completed record from Enosh #11 as well.
            from enoch.tasks.queue import task_queue_path
            import json
            path = task_queue_path(root)
            data = json.loads(path.read_text())
            data["history"][-1]["publish_stage"] = "review_published"
            data["history"][-1]["review_published"] = True
            path.write_text(json.dumps(data))
            write_section_value("task", "require_remote_review", "true", root)
            app.review = IndependentReviewFixture()
            # The fixture removes workspace metadata but leaves its files on disk.
            # Simulate real successful cleanup before reconstructing the workspace.
            import shutil
            shutil.rmtree(original.workspace_path)
            app._retry_task(original.id)
            retried = app.workflow.inspect().pending[0]
            self.assertEqual(retried.publish_stage, "captured")
            self.assertFalse(retried.review_published)
            self.run_retry(app, retried)
            completed = app.workflow.find(retried.id)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.revision_id, original.revision_id)
            self.assertTrue(completed.review_url)
            self.assertEqual(len(runtime.calls), 1)
            with self.assertRaises(TaskRetryError):
                app.workflow.retry_failed(completed.id)

    def test_existing_reference_failure_is_not_reported_completed_and_retries_without_runtime(self):
        for response in ("local", "unpublished", "missing-url", "exception"):
            with self.subTest(response=response), TemporaryDirectory() as temp:
                app, repository, runtime = self.application(Path(temp), required=False)
                revision = RepositoryRevision("existing-revision")
                repository.revisions[revision.id] = revision
                repository.revisions["feature/existing"] = revision
                repository.parents[revision.id] = repository.authoritative.id
                if response != "local":
                    review = IndependentReviewFixture()
                    original_publish = review.publish_review
                    def incomplete(request, root=None):
                        if response == "exception":
                            from enoch.providers import ReviewProviderError
                            raise ReviewProviderError("HTTP 503: service unavailable")
                        result = original_publish(request, root)
                        return replace(result, state="unpublished") if response == "unpublished" else replace(
                            result, identity=replace(result.identity, url=""))
                    review.publish_review = incomplete
                    app.review = review
                failed = self.run_task(app, "publish existing local branch `feature/existing` as a PR against `main`")
                self.assertNotEqual(failed.status, "completed")
                self.assertEqual(failed.publish_stage, "captured")
                self.assertEqual(failed.revision_id, revision.id)
                self.assertFalse(failed.review_published)
                self.assertTrue(repository.workspaces)
                self.assertEqual(len(runtime.calls), 0)
                app.review = IndependentReviewFixture()
                if failed.status == "pending":
                    retried = failed  # Automatic retry of a transient provider failure.
                else:
                    retried = app.workflow.retry_failed(failed.id)
                self.run_retry(app, retried)
                completed = app.workflow.find(retried.id)
                self.assertEqual(completed.status, "completed")
                self.assertTrue(completed.review_published)
                self.assertTrue(completed.review_url)
                self.assertEqual(completed.revision_id, revision.id)
                self.assertEqual(len(runtime.calls), 0)
                self.assertFalse(repository.workspaces)

    def test_remote_provider_cannot_be_downgraded_by_false_setting(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            review = UnpublishedReview()
            review.supports_remote_review = True
            app, _, _ = self.application(root, required=False, review=review)
            write_section_value("task", "require_remote_review", "false", root)
            failed = self.run_task(app)
            self.assertNotEqual(failed.status, "completed")
            self.assertEqual(failed.failure_code, "review_publication_failed")
            self.assertFalse(failed.review_published)

    def test_retry_refuses_changed_captured_workspace(self):
        with TemporaryDirectory() as temp:
            app, repository, runtime = self.application(Path(temp))
            failed = self.run_task(app)
            repository.mark_changed("unrelated.txt")
            app.review = IndependentReviewFixture()
            retried = app.workflow.retry_failed(failed.id)
            self.run_retry(app, retried)
            result = app.workflow.find(retried.id)
            self.assertNotEqual(result.status, "completed")
            self.assertFalse(app.review.reviews)
            self.assertEqual(len(runtime.calls), 1)
            self.assertTrue(repository.workspaces)


if __name__ == "__main__":
    unittest.main()
