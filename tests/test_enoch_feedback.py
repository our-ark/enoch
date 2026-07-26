from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.evolution.evidence import (
    load_evidence,
    pending_evidence_counts,
    save_evidence_batch_size,
    scan_evidence,
)
from enoch.logs import log_conversation_turn


class EnochFeedbackEvidenceTests(unittest.TestCase):
    def test_scans_configured_message_batch_semantically(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            save_evidence_batch_size("feedback", 2, root)
            log_conversation_turn(
                chat_id=42,
                message="The first message is ordinary context.",
                reply="Context received.",
                root=root,
            )
            log_conversation_turn(
                chat_id=42,
                message="Please make task errors easier to understand.",
                reply="I can improve that workflow.",
                root=root,
            )
            prompts: list[str] = []

            def generate(prompt: str) -> str:
                prompts.append(prompt)
                records = json.loads(prompt.split("Evidence input: ", 1)[1])
                return json.dumps(
                    [
                        {
                            "observation": "Task errors are difficult to understand.",
                            "evidence_type": "usability feedback",
                            "affected_area": "task failure messages",
                            "desired_outcome": "Failures explain the actionable cause clearly.",
                            "confidence": 0.95,
                            "explicit": True,
                            "evidence_refs": [records[1]["ref"]],
                        }
                    ]
                )

            result = scan_evidence("feedback", root, generator=generate)
            evidence = load_evidence(root)
            pending = pending_evidence_counts(root)["feedback"]

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.processed, 2)
        self.assertEqual(len(evidence), 1)
        self.assertTrue(evidence[0].explicit)
        self.assertIn("The first message is ordinary context.", prompts[0])
        self.assertIn("Context received.", prompts[0])
        self.assertEqual(pending, 0)

    def test_waits_for_threshold_and_valid_empty_result_advances_cursor(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            save_evidence_batch_size("feedback", 2, root)
            log_conversation_turn(
                chat_id=42,
                message="Tell me about the system.",
                reply="Here is the system.",
                root=root,
            )

            waiting = scan_evidence(
                "feedback",
                root,
                generator=lambda _prompt: self.fail("threshold should not call Codex"),
            )
            completed = scan_evidence(
                "feedback",
                root,
                generator=lambda _prompt: "[]",
                force=True,
            )

        self.assertEqual(waiting.status, "waiting")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(load_evidence(root), ())
        self.assertEqual(pending_evidence_counts(root)["feedback"], 0)

    def test_invalid_response_leaves_inputs_pending_and_redacts_tokens(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            save_evidence_batch_size("feedback", 1, root)
            log_conversation_turn(
                chat_id=42,
                message=(
                    "bin/enoch setup token "
                    "8937129711:AAFwQKwwfj6DtoiFlr6ypIr2kmiLxJQQkJM"
                ),
                reply="Token saved.",
                root=root,
            )
            prompts: list[str] = []

            result = scan_evidence(
                "feedback",
                root,
                generator=lambda prompt: prompts.append(prompt) or "not json",
            )
            pending = pending_evidence_counts(root)["feedback"]

        self.assertEqual(result.status, "failed")
        self.assertEqual(pending, 1)
        self.assertNotIn("AAFwQKww", prompts[0])
        self.assertIn("[redacted]", prompts[0])

    def test_rejects_json_wrapped_in_prose(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            save_evidence_batch_size("feedback", 1, root)
            log_conversation_turn(
                chat_id=42,
                message="Ordinary conversation.",
                reply="Ordinary reply.",
                root=root,
            )

            result = scan_evidence(
                "feedback",
                root,
                generator=lambda _prompt: "Here is the JSON:\n[]",
            )
            pending = pending_evidence_counts(root)["feedback"]

        self.assertEqual(result.status, "failed")
        self.assertEqual(pending, 1)


if __name__ == "__main__":
    unittest.main()
