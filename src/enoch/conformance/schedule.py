from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from enoch.extensions.schedules import (
    ExtensionScheduleSpec,
    all_extension_schedule_statuses,
    claim_due_extension_schedules,
    pause_extension_schedule,
    reconcile_extension_schedules,
    record_extension_schedule_task,
    request_extension_schedule_run,
    resume_extension_schedule,
)


class ExtensionScheduleConformanceMixin:
    """Deterministic, network-free checks for extension schedule semantics."""

    def test_conformance_extension_schedule_timezone_and_missed_runs(self) -> None:
        start = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)
        spec = ExtensionScheduleSpec(
            "daily-refresh",
            "Refresh the research index",
            daily_time="09:00",
            timezone="America/Los_Angeles",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            created = reconcile_extension_schedules(
                {"researcher": (spec,)},
                root,
                now=start,
            )[0]
            claimed = claim_due_extension_schedules(
                root,
                now=datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc),
            )[0]
            acknowledged = record_extension_schedule_task(
                claimed.id,
                7,
                root,
                claim_id=claimed.claim_id,
                now=datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(created.next_run_at, "2026-07-31T16:00:00+00:00")
        self.assertEqual(claimed.claim_scheduled_for, created.next_run_at)
        self.assertIsNotNone(acknowledged)
        self.assertEqual(
            acknowledged.next_run_at,
            "2026-08-03T16:00:00+00:00",
        )

    def test_conformance_extension_schedule_pause_run_now_and_restart_dedup(
        self,
    ) -> None:
        start = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        spec = ExtensionScheduleSpec(
            "refresh",
            "Refresh project state",
            interval_seconds=3600,
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            reconcile_extension_schedules({"manager": (spec,)}, root, now=start)
            paused = pause_extension_schedule(
                "manager",
                "refresh",
                root,
                now=start,
            )
            self.assertEqual(
                claim_due_extension_schedules(
                    root,
                    now=datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc),
                ),
                (),
            )
            resumed = resume_extension_schedule(
                "manager",
                "refresh",
                root,
                now=datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc),
            )
            requested = request_extension_schedule_run(
                "manager",
                "refresh",
                root,
                now=datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc),
                idempotency_key="manual-refresh-1",
            )
            first_claim = claim_due_extension_schedules(
                root,
                now=datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc),
            )[0]
            restarted_claim = claim_due_extension_schedules(
                root,
                now=datetime(2026, 7, 31, 14, 1, tzinfo=timezone.utc),
            )[0]
            acknowledged = record_extension_schedule_task(
                first_claim.id,
                8,
                root,
                claim_id=first_claim.claim_id,
                now=datetime(2026, 7, 31, 14, 1, tzinfo=timezone.utc),
            )
            duplicate_request = request_extension_schedule_run(
                "manager",
                "refresh",
                root,
                now=datetime(2026, 7, 31, 14, 2, tzinfo=timezone.utc),
                idempotency_key="manual-refresh-1",
            )

        self.assertEqual(paused.state, "paused")
        self.assertEqual(resumed.state, "active")
        self.assertTrue(requested.run_now_id)
        self.assertEqual(first_claim.claim_id, restarted_claim.claim_id)
        self.assertEqual(first_claim.claim_kind, "scheduled")
        self.assertIsNotNone(acknowledged)
        self.assertEqual(acknowledged.next_run_at, "2026-07-31T15:00:00+00:00")
        self.assertIn("manual-refresh-1", duplicate_request.run_now_history)
        self.assertFalse(duplicate_request.run_now_id)

    def test_conformance_extension_schedule_disable_preserves_history(self) -> None:
        start = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        spec = ExtensionScheduleSpec(
            "refresh",
            "Refresh project state",
            interval_seconds=600,
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            reconcile_extension_schedules({"manager": (spec,)}, root, now=start)
            disabled = reconcile_extension_schedules(
                {},
                root,
                now=datetime(2026, 7, 31, 12, 5, tzinfo=timezone.utc),
            )[0]
            due = claim_due_extension_schedules(
                root,
                now=datetime(2026, 7, 31, 13, 0, tzinfo=timezone.utc),
            )
            restored = all_extension_schedule_statuses(root)[0]

        self.assertEqual(disabled.state, "disabled")
        self.assertTrue(disabled.disabled_at)
        self.assertEqual(due, ())
        self.assertEqual(restored.id, "extension:manager:refresh")
        self.assertEqual(restored.state, "disabled")
