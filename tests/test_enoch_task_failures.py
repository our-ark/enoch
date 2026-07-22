from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.tasks.failures import classify_task_failure


class EnochTaskFailureTests(unittest.TestCase):
    def test_dirty_worktree_is_non_retryable(self) -> None:
        failure = classify_task_failure(
            "Enoch could not complete the requested work yet: "
            "Worktree is not clean. Commit, stash, or discard changes before evolving."
        )

        self.assertEqual(failure.code, "dirty_worktree")
        self.assertEqual(failure.failure_class, "permanent")
        self.assertFalse(failure.retryable)

    def test_validation_and_task_timeout_are_non_retryable(self) -> None:
        validation = classify_task_failure(
            "I did not open a PR because doctor failed."
        )
        timeout = classify_task_failure(
            "Task exceeded the configured 10m timeout."
        )

        self.assertEqual(validation.code, "validation_failed")
        self.assertFalse(validation.retryable)
        self.assertEqual(timeout.code, "task_timeout")
        self.assertFalse(timeout.retryable)

    def test_transient_network_and_service_failures_are_retryable(self) -> None:
        network = classify_task_failure("Enoch could not continue: connection reset by peer.")
        service = classify_task_failure("Enoch could not continue: HTTP 503 service unavailable.")

        self.assertEqual(network.code, "network_error")
        self.assertTrue(network.retryable)
        self.assertEqual(service.code, "service_unavailable")
        self.assertTrue(service.retryable)

    def test_unknown_failure_defaults_to_non_retryable(self) -> None:
        failure = classify_task_failure("Enoch could not finish for an unfamiliar reason.")

        self.assertEqual(failure.code, "unknown_failure")
        self.assertEqual(failure.failure_class, "permanent")
        self.assertFalse(failure.retryable)

    def test_missing_runtime_is_specific_and_non_retryable(self) -> None:
        failure = classify_task_failure(
            "Enoch cannot find the Codex CLI. Configure it in Enoch config."
        )

        self.assertEqual(failure.code, "runtime_not_found")
        self.assertEqual(failure.failure_class, "permanent")
        self.assertFalse(failure.retryable)


if __name__ == "__main__":
    unittest.main()
