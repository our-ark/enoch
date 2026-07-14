from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.feedback import extract_feedback_signals
from enoch.logs import log_conversation_turn


class EnochFeedbackTests(unittest.TestCase):
    def test_extracts_correction_preference_complaint_and_repetition(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            messages = [
                "No, remove the automatic merge behavior.",
                "我希望报告保持单栏。",
                "The recovery command is broken.",
                "Please show the candidate provenance.",
                "Please show the candidate provenance.",
            ]
            for message in messages:
                log_conversation_turn(chat_id=42, message=message, reply="ok", root=root)

            signals = extract_feedback_signals(root)

        kinds = {signal.kind for signal in signals}
        self.assertEqual(kinds, {"correction", "preference", "complaint", "repeated-request"})
        repeated = next(signal for signal in signals if signal.kind == "repeated-request")
        self.assertEqual(repeated.occurrences, 2)

    def test_ignores_commands_and_neutral_single_messages(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            log_conversation_turn(chat_id=42, message="/status", reply="ok", root=root)
            log_conversation_turn(chat_id=42, message="Tell me about the system.", reply="ok", root=root)

            signals = extract_feedback_signals(root)

        self.assertEqual(signals, ())


if __name__ == "__main__":
    unittest.main()
