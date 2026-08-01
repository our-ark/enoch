from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.conformance import ExtensionScheduleConformanceMixin
from enoch.extensions import (
    ExtensionArtifactReference,
    ExtensionScheduleControlError,
    ExtensionScheduleError,
    ExtensionScheduleSpec,
    ExtensionSchedules,
)
from enoch.extensions.schedules import (
    claim_due_extension_schedules,
    find_extension_schedule,
    pause_extension_schedule,
    reconcile_extension_schedules,
    request_extension_schedule_run,
    resume_extension_schedule,
)


class EnochExtensionScheduleConformanceTests(
    ExtensionScheduleConformanceMixin,
    unittest.TestCase,
):
    pass


class EnochExtensionScheduleTests(unittest.TestCase):
    def test_schedule_spec_is_bounded_and_normalized(self) -> None:
        spec = ExtensionScheduleSpec(
            " Daily-Refresh ",
            " Refresh   the index ",
            daily_time="09:30",
            timezone="America/Los_Angeles",
            required_capabilities=("runtime.execute", "runtime.execute"),
            metadata={"contract_version": 1},
            artifact_refs=(
                ExtensionArtifactReference(
                    "research-index",
                    "indexes/current.json",
                    "application/json",
                ),
            ),
            lane=" Daily ",
        )

        self.assertEqual(spec.name, "daily-refresh")
        self.assertEqual(spec.request, "Refresh the index")
        self.assertEqual(spec.required_capabilities, ("runtime.execute",))
        self.assertEqual(spec.lane, "daily")
        self.assertEqual(spec.cadence, "daily")

        invalid = (
            {"interval_seconds": 59},
            {"interval_seconds": 60, "daily_time": "09:30"},
            {"daily_time": "25:00"},
            {"daily_time": "09:30", "timezone": "Mars/Olympus"},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(
                ExtensionScheduleError
            ):
                ExtensionScheduleSpec("refresh", "Refresh", **values)

    def test_schedule_upgrade_preserves_or_reanchors_next_occurrence(self) -> None:
        start = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            initial = reconcile_extension_schedules(
                {
                    "manager": (
                        ExtensionScheduleSpec(
                            "refresh",
                            "Refresh v1",
                            interval_seconds=3600,
                        ),
                    )
                },
                root,
                now=start,
            )[0]
            request_upgrade = reconcile_extension_schedules(
                {
                    "manager": (
                        ExtensionScheduleSpec(
                            "refresh",
                            "Refresh v2",
                            interval_seconds=3600,
                        ),
                    )
                },
                root,
                now=datetime(2026, 7, 31, 12, 10, tzinfo=timezone.utc),
            )[0]
            cadence_upgrade = reconcile_extension_schedules(
                {
                    "manager": (
                        ExtensionScheduleSpec(
                            "refresh",
                            "Refresh v2",
                            interval_seconds=7200,
                        ),
                    )
                },
                root,
                now=datetime(2026, 7, 31, 12, 20, tzinfo=timezone.utc),
            )[0]

        self.assertEqual(request_upgrade.next_run_at, initial.next_run_at)
        self.assertEqual(cadence_upgrade.next_run_at, "2026-07-31T14:20:00+00:00")

    def test_control_surface_is_extension_scoped(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            reconcile_extension_schedules(
                {
                    "manager": (
                        ExtensionScheduleSpec(
                            "refresh",
                            "Refresh state",
                            interval_seconds=600,
                        ),
                    )
                },
                root,
            )
            researcher = ExtensionSchedules("researcher", root)

            with self.assertRaises(ExtensionScheduleControlError) as raised:
                researcher.pause("refresh")

            manager_status = find_extension_schedule("manager", "refresh", root)

        self.assertEqual(raised.exception.code, "schedule_not_found")
        self.assertIsNotNone(manager_status)
        self.assertEqual(manager_status.state, "active")

    def test_pause_and_disable_preserve_an_inflight_claim(self) -> None:
        spec = ExtensionScheduleSpec(
            "refresh",
            "Refresh state",
            interval_seconds=3600,
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            reconcile_extension_schedules({"manager": (spec,)}, root)
            request_extension_schedule_run("manager", "refresh", root)
            claimed = claim_due_extension_schedules(root)[0]
            paused = pause_extension_schedule("manager", "refresh", root)
            while_paused = claim_due_extension_schedules(root)
            resumed = resume_extension_schedule("manager", "refresh", root)
            reclaimed = claim_due_extension_schedules(root)[0]
            disabled = reconcile_extension_schedules({}, root)[0]
            while_disabled = claim_due_extension_schedules(root)
            reenabled = reconcile_extension_schedules(
                {"manager": (spec,)},
                root,
            )[0]
            recovered = claim_due_extension_schedules(root)[0]

        self.assertEqual(paused.claim_id, claimed.claim_id)
        self.assertEqual(while_paused, ())
        self.assertEqual(resumed.claim_id, claimed.claim_id)
        self.assertEqual(reclaimed.claim_id, claimed.claim_id)
        self.assertEqual(disabled.claim_id, claimed.claim_id)
        self.assertEqual(while_disabled, ())
        self.assertEqual(reenabled.claim_id, claimed.claim_id)
        self.assertEqual(recovered.claim_id, claimed.claim_id)


if __name__ == "__main__":
    unittest.main()
