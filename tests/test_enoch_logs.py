from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.logs import conversation_log_path, log_conversation_turn, log_system_event, system_log_path


class EnochLogsTests(unittest.TestCase):
    def test_conversation_turns_are_written_to_daily_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            path = log_conversation_turn(chat_id=42, message="hello", reply="hi", root=root)

            self.assertEqual(path, root.resolve() / ".enoch" / "logs" / "conversations" / _today_name())
            records = _read_jsonl(path)
            self.assertEqual(records[0]["channel"], "chat")
            self.assertEqual(records[0]["chat_id"], 42)
            self.assertEqual(records[0]["message"], "hello")
            self.assertEqual(records[0]["reply"], "hi")

    def test_system_events_are_written_to_daily_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            path = log_system_event("startup", status="ok", details={"pid": 123}, root=root)

            self.assertEqual(path, root.resolve() / ".enoch" / "logs" / "system" / _today_name())
            records = _read_jsonl(path)
            self.assertEqual(records[0]["event"], "startup")
            self.assertEqual(records[0]["status"], "ok")
            self.assertEqual(records[0]["details"], {"pid": 123})

    def test_log_paths_can_be_computed_for_specific_days(self) -> None:
        when = datetime(2026, 6, 19, tzinfo=timezone.utc)

        self.assertEqual(
            conversation_log_path(ROOT, when=when),
            ROOT / ".enoch" / "logs" / "conversations" / "2026-06-19.jsonl",
        )
        self.assertEqual(
            system_log_path(ROOT, when=when),
            ROOT / ".enoch" / "logs" / "system" / "2026-06-19.jsonl",
        )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _today_name() -> str:
    return f"{datetime.now(timezone.utc).date().isoformat()}.jsonl"


if __name__ == "__main__":
    unittest.main()
