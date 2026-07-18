from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.experience import experience_path, load_experience_records, record_task_experience
from enoch.task_queue import TaskJob


class EnochExperienceTests(unittest.TestCase):
    def test_records_structured_terminal_task_experience(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = _task_job(
                7,
                status="completed",
                result="\n".join(
                    [
                        "Completed the change.",
                        "Files:",
                        "- src/enoch/telegram/bot.py",
                    ]
                ),
                pr_urls=("https://github.com/our-ark/enoch/pull/42",),
            )

            record = record_task_experience(job, root, command="/task")
            loaded = load_experience_records(root)

        self.assertEqual(record.id, "task-7")
        self.assertEqual(record.outcome, "completed")
        self.assertTrue(record.started)
        self.assertEqual(record.changed_files, ("src/enoch/telegram/bot.py",))
        self.assertEqual(record.pr_urls, ("https://github.com/our-ark/enoch/pull/42",))
        self.assertEqual(loaded, (record,))

    def test_deduplicates_repeated_writes_for_same_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = _task_job(3, status="cancelled", result="Stopped by /stop.")

            first = record_task_experience(job, root, command="/stop")
            second = record_task_experience(job, root, command="/task")
            lines = experience_path(root).read_text(encoding="utf-8").splitlines()

        self.assertEqual(first, second)
        self.assertEqual(len(lines), 1)

    def test_rejects_non_terminal_task(self) -> None:
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "completed, failed, or cancelled"):
                record_task_experience(_task_job(1, status="running"), Path(temp))


def _task_job(
    task_id: int,
    *,
    status: str,
    result: str = "",
    pr_urls: tuple[str, ...] = (),
) -> TaskJob:
    return TaskJob(
        id=task_id,
        chat_id=42,
        text="improve the Telegram workflow",
        created_at="2026-07-18T00:00:00+00:00",
        started_at="2026-07-18T00:01:00+00:00",
        completed_at="2026-07-18T00:02:00+00:00",
        status=status,
        result=result,
        pr_urls=pr_urls,
        context_source="chat-snapshot",
    )


if __name__ == "__main__":
    unittest.main()
