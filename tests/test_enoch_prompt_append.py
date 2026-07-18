import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.prompt_append import (
    EDIT_REQUEST_END,
    EDIT_REQUEST_START,
    MEMORY_REQUEST_END,
    MEMORY_REQUEST_START,
    TASK_REGRESSION_END,
    TASK_REGRESSION_START,
    extract_edit_request,
    extract_memory_requests,
    extract_task_regression_signals,
    read_only_turn_prompt,
    repository_handoff_note,
    work_request_prompt,
)


class EnochPromptAppendTests(unittest.TestCase):
    def test_read_only_prompt_points_work_to_commands(self) -> None:
        prompt = read_only_turn_prompt("make the CLI clearer")

        self.assertIn("make the CLI clearer", prompt)
        self.assertIn("You are in read-only mode.", prompt)
        self.assertIn("Do not request an automatic edit", prompt)
        self.assertIn("/do", prompt)
        self.assertIn("/task", prompt)
        self.assertIn("/backlog", prompt)
        self.assertNotIn(EDIT_REQUEST_START, prompt)
        self.assertIn(MEMORY_REQUEST_START, prompt)
        self.assertIn(TASK_REGRESSION_START, prompt)
        self.assertIn("Enoch owns regression bookkeeping", prompt)
        self.assertNotIn("Roy", prompt)

    def test_work_request_prompt_allows_complete_jobs(self) -> None:
        prompt = work_request_prompt("Update README.")

        self.assertIn("Proceed with this work request:", prompt)
        self.assertIn("Update README.", prompt)
        self.assertIn("Complete the requested work directly.", prompt)
        self.assertIn("creating a pull request", prompt)
        self.assertIn("forward-fixed", prompt)

    def test_extract_edit_request_strips_marker_from_visible_reply(self) -> None:
        reply = f"I can do that.\n\n{EDIT_REQUEST_START}\nUpdate README.\n{EDIT_REQUEST_END}"

        request = extract_edit_request(reply)

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.visible_reply, "I can do that.")
        self.assertEqual(request.request, "Update README.")

    def test_extract_memory_requests_strips_markers_from_visible_reply(self) -> None:
        reply = (
            "I will remember that.\n\n"
            f"{MEMORY_REQUEST_START}\nUser likes apples.\n{MEMORY_REQUEST_END}\n\n"
            f"{MEMORY_REQUEST_START}\nProject prefers PRs.\n{MEMORY_REQUEST_END}"
        )

        result = extract_memory_requests(reply)

        self.assertEqual(result.visible_reply, "I will remember that.")
        self.assertEqual(result.requests, ("User likes apples.", "Project prefers PRs."))

    def test_extract_task_regression_signal_strips_internal_json(self) -> None:
        reply = (
            "The rollback is complete.\n\n"
            f"{TASK_REGRESSION_START}\n"
            '{"task_id": 7, "reason": "Deploy check failed.", "resolution": "reverted"}\n'
            f"{TASK_REGRESSION_END}"
        )

        result = extract_task_regression_signals(reply)

        self.assertEqual(result.visible_reply, "The rollback is complete.")
        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].task_id, 7)
        self.assertEqual(result.signals[0].reason, "Deploy check failed.")
        self.assertEqual(result.signals[0].resolution, "reverted")

    def test_invalid_task_regression_signal_is_hidden_and_ignored(self) -> None:
        reply = (
            "I could not identify the task.\n"
            f"{TASK_REGRESSION_START}\nnot-json\n{TASK_REGRESSION_END}"
        )

        result = extract_task_regression_signals(reply)

        self.assertEqual(result.visible_reply, "I could not identify the task.")
        self.assertEqual(result.signals, ())

    def test_repository_handoff_note_warns_not_to_assume_merge(self) -> None:
        note = repository_handoff_note("enoch/turn-readme", "https://github.com/our-ark/enoch/pull/1")

        self.assertIn("enoch/turn-readme", note)
        self.assertIn("Do not assume the PR was merged.", note)
        self.assertIn("Local checkout is back on `main`.", note)


if __name__ == "__main__":
    unittest.main()
