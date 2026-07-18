from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.experience import experience_path, load_experience_records, record_task_experience
from enoch.task_events import TASK_EVENT_TYPES, TASK_SOURCES, load_task_events, task_event_path
from enoch.task_queue import (
    TaskJob,
    begin_next_task,
    complete_task,
    enqueue_task,
    regress_task,
    resolve_regressed_task,
)


class EnochExperienceTests(unittest.TestCase):
    def test_declares_eight_task_sources_and_full_terminal_lifecycle(self) -> None:
        self.assertEqual(
            TASK_SOURCES,
            {
                "backlog",
                "feedback",
                "experience",
                "inheritance",
                "learning",
                "brainstorming",
                "task",
                "chat-task",
            },
        )
        self.assertTrue(
            {
                "completed",
                "failed",
                "cancelled",
                "regressed",
                "reverted",
                "forward-fixed",
            }
            <= TASK_EVENT_TYPES
        )

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
            events = load_task_events(root, task_id=7)

        self.assertEqual(record.id, "task-7")
        self.assertEqual(record.outcome, "completed")
        self.assertTrue(record.started)
        self.assertEqual(record.changed_files, ("src/enoch/telegram/bot.py",))
        self.assertEqual(record.pr_urls, ("https://github.com/our-ark/enoch/pull/42",))
        self.assertEqual(record.source, "task")
        self.assertEqual(record.initiated_by, "human")
        self.assertEqual(loaded, (record,))
        self.assertEqual(
            [event.event for event in events],
            ["created", "started", "completed"],
        )

    def test_deduplicates_repeated_terminal_compatibility_writes(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = _task_job(3, status="cancelled", result="Stopped by /stop.")

            first = record_task_experience(job, root, command="/stop")
            second = record_task_experience(job, root, command="/task")
            lines = task_event_path(root).read_text(encoding="utf-8").splitlines()

        self.assertEqual(first, second)
        self.assertEqual(len(lines), 3)

    def test_rejects_non_terminal_task(self) -> None:
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "regressed, reverted, or forward-fixed"):
                record_task_experience(_task_job(1, status="running"), Path(temp))

    def test_preserves_regression_fact_after_forward_fix_resolution(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "ship risky work", root)
            complete_task(begin_next_task(root).id, root, result="Shipped.")
            regress_task(original.id, root, result="Broke recovery.")
            fix = enqueue_task(42, "repair recovery", root, parent_task_id=original.id)
            complete_task(begin_next_task(root).id, root, result="Fixed.")
            resolve_regressed_task(
                original.id,
                "forward-fixed",
                root,
                result="Verified repaired.",
                related_task_id=fix.id,
            )

            records = {record.task_id: record for record in load_experience_records(root)}
            original_record = records[original.id]

        self.assertEqual(original_record.outcome, "forward-fixed")
        self.assertTrue(original_record.regressed)
        self.assertEqual(original_record.regression_resolution, "forward-fixed")
        self.assertEqual(original_record.regression_related_task_id, fix.id)

    def test_reads_legacy_experience_records_when_event_log_is_absent(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = experience_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"task_id":9,"outcome":"failed","request":"legacy task",'
                '"created_at":"2026-07-18T00:00:00+00:00","command":"/task",'
                '"result_summary":"failed before migration","started":true}\n',
                encoding="utf-8",
            )

            records = load_experience_records(root)

        self.assertEqual(records[0].task_id, 9)
        self.assertEqual(records[0].outcome, "failed")
        self.assertEqual(records[0].source, "task")
        self.assertEqual(records[0].initiated_by, "human")


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
