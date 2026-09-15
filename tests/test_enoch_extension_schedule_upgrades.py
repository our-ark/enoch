"""Old persisted targets meeting the shared schedule calculator.

The calculator resolves a wall-clock time inside a spring-forward gap to the
first instant after the jump; the implementation it replaced landed a step past
it. These tests pin what happens to a target the previous implementation already
wrote: reconciliation keeps it until the occurrence is acknowledged, and only
the target calculated afterwards follows the current rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.extensions import ExtensionScheduleSpec
from enoch.extensions.schedules import (
    claim_due_extension_schedules,
    extension_schedule_path,
    find_extension_schedule,
    reconcile_extension_schedules,
    record_extension_schedule_task,
)
from enoch.schedules import next_daily_run


PACIFIC = "America/Los_Angeles"
# 2026-03-08 02:30 never happens in the Pacific zone. The previous calculator
# read it with the pre-jump offset and persisted 03:30 PDT; the shared one
# resolves it to 03:00 PDT, the first instant the jump landed on.
LEGACY_TARGET = "2026-03-08T10:30:00+00:00"
SHARED_TARGET = "2026-03-08T10:00:00+00:00"


def _spec(request: str = "Refresh state", daily_time: str = "02:30") -> ExtensionScheduleSpec:
    return ExtensionScheduleSpec(
        "refresh",
        request,
        daily_time=daily_time,
        timezone=PACIFIC,
    )


def _seed_legacy_state(root: Path) -> None:
    """Persist the record the previous calculator would have left behind."""

    reconcile_extension_schedules(
        {"manager": (_spec(),)},
        root,
        now=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
    )
    path = extension_schedule_path(root)
    payload = json.loads(path.read_text())
    for item in payload["schedules"]:
        item["next_run_at"] = LEGACY_TARGET
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


class EnochExtensionScheduleUpgradeTests(unittest.TestCase):
    def test_gap_target_is_recalculated_only_once_acknowledged(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _seed_legacy_state(root)

            unchanged = reconcile_extension_schedules(
                {"manager": (_spec(),)},
                root,
                now=datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc),
            )[0]
            request_only = reconcile_extension_schedules(
                {"manager": (_spec("Refresh state twice over"),)},
                root,
                now=datetime(2026, 3, 7, 20, 0, tzinfo=timezone.utc),
            )[0]

            acknowledged_at = datetime(2026, 3, 8, 10, 31, tzinfo=timezone.utc)
            claimed = claim_due_extension_schedules(root, now=acknowledged_at)[0]
            record_extension_schedule_task(
                claimed.id,
                1,
                root,
                claim_id=claimed.claim_id,
                now=acknowledged_at,
            )
            after_acknowledgement = find_extension_schedule("manager", "refresh", root)

        # The declaration did not change how the target is calculated, so the
        # occurrence the previous calculator wrote stands and is claimed at its
        # own time rather than at the shared calculator's 10:00Z.
        self.assertEqual(unchanged.next_run_at, LEGACY_TARGET)
        self.assertEqual(request_only.next_run_at, LEGACY_TARGET)
        self.assertEqual(claimed.claim_kind, "scheduled")
        self.assertEqual(claimed.claim_scheduled_for, LEGACY_TARGET)
        self.assertNotEqual(LEGACY_TARGET, SHARED_TARGET)
        # Acknowledging it hands the schedule back to the shared calculator.
        self.assertEqual(
            after_acknowledgement.next_run_at,
            next_daily_run("02:30", PACIFIC, acknowledged_at).isoformat(),
        )

    def test_a_cadence_change_adopts_the_current_gap_rule(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _seed_legacy_state(root)

            # 02:45 is inside the same gap, so the recalculated target shows the
            # rule in force rather than an ordinary wall-clock answer.
            retimed = reconcile_extension_schedules(
                {"manager": (_spec(daily_time="02:45"),)},
                root,
                now=datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc),
            )[0]

        self.assertEqual(retimed.daily_time, "02:45")
        self.assertEqual(retimed.next_run_at, SHARED_TARGET)


if __name__ == "__main__":
    unittest.main()
