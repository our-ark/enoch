from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.cron import (
    add_cron_job,
    cancel_cron_job,
    claim_due_cron_jobs,
    cron_status,
    format_cron_interval,
    parse_cron_interval,
    record_cron_task,
)


class EnochCronTests(unittest.TestCase):
    def test_parse_and_format_intervals(self) -> None:
        self.assertEqual(parse_cron_interval("10m"), 600)
        self.assertEqual(parse_cron_interval("2 hours"), 7200)
        self.assertEqual(parse_cron_interval("1d"), 86400)
        self.assertEqual(format_cron_interval(600), "10m")
        self.assertEqual(format_cron_interval(7200), "2h")

    def test_add_cron_job_persists_context_and_next_run(self) -> None:
        current = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as temp:
            root = Path(temp)

            job = add_cron_job(
                42,
                "run scheduled work",
                600,
                root,
                context="Use saved context.",
                context_source="chat-snapshot",
                now=current,
            )
            status = cron_status(root)

        self.assertEqual(job.id, 1)
        self.assertEqual(job.next_run_at, "2026-06-30T12:10:00+00:00")
        self.assertEqual(status.active, (job,))
        self.assertEqual(status.active[0].context, "Use saved context.")
        self.assertEqual(status.active[0].context_source, "chat-snapshot")

    def test_due_job_remains_claimed_until_task_is_recorded(self) -> None:
        start = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
        due = datetime(2026, 6, 30, 12, 10, tzinfo=timezone.utc)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = add_cron_job(42, "run scheduled work", 600, root, now=start)

            claimed = claim_due_cron_jobs(root, now=due)
            claimed_again = claim_due_cron_jobs(root, now=due)
            metadata_only = record_cron_task(job.id, 6, root, now=due)
            still_claimed = claim_due_cron_jobs(root, now=due)
            recorded = record_cron_task(
                job.id,
                7,
                root,
                claim_id=claimed[0].claim_id,
                now=due,
            )
            after_ack = claim_due_cron_jobs(root, now=due)
            status = cron_status(root)

        self.assertEqual([item.id for item in claimed], [job.id])
        self.assertEqual(claimed_again[0].claim_id, claimed[0].claim_id)
        self.assertEqual(metadata_only.last_task_id, 6)
        self.assertEqual(still_claimed[0].claim_id, claimed[0].claim_id)
        self.assertIsNotNone(recorded)
        self.assertEqual(after_ack, ())
        self.assertEqual(status.active[0].last_run_at, "2026-06-30T12:10:00+00:00")
        self.assertEqual(status.active[0].next_run_at, "2026-06-30T12:20:00+00:00")

    def test_cancel_and_record_last_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = add_cron_job(42, "run scheduled work", 60, root)

            updated = record_cron_task(job.id, 7, root)
            cancelled = cancel_cron_job(job.id, root)
            status = cron_status(root)

        self.assertEqual(updated.last_task_id, 7)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.last_task_id, 7)
        self.assertEqual(status.active, ())
        self.assertEqual(status.history[-1].id, job.id)


if __name__ == "__main__":
    unittest.main()
