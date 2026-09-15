from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.schedules import (
    ScheduleError,
    next_daily_run,
    next_interval_run,
    normalize_daily_time,
    normalize_timezone,
)


PACIFIC = "America/Los_Angeles"
LORD_HOWE = "Australia/Lord_Howe"


class EnochScheduleNormalizationTests(unittest.TestCase):
    def test_daily_time_requires_a_wall_clock_value(self) -> None:
        self.assertEqual(normalize_daily_time(" 09:30 "), "09:30")
        self.assertEqual(normalize_daily_time(""), "")
        self.assertEqual(normalize_daily_time(None), "")

        for value in ("25:00", "9:30", "09:60", "0930", "09:30:00"):
            with self.subTest(value=value), self.assertRaises(ScheduleError):
                normalize_daily_time(value)
        with self.assertRaises(ScheduleError):
            normalize_daily_time(930)

    def test_timezone_must_be_a_resolvable_iana_name(self) -> None:
        self.assertEqual(normalize_timezone(" UTC "), "UTC")
        self.assertEqual(normalize_timezone(PACIFIC), PACIFIC)

        for value in ("", "   ", "Mars/Olympus", "x" * 129, None):
            with self.subTest(value=value), self.assertRaises(ScheduleError):
                normalize_timezone(value)

    def test_messages_adopt_the_caller_label(self) -> None:
        with self.assertRaises(ScheduleError) as invalid_time:
            normalize_daily_time("25:00", label="Extension schedule")
        with self.assertRaises(ScheduleError) as unknown_zone:
            normalize_timezone("Mars/Olympus", label="Extension schedule")

        self.assertEqual(
            str(invalid_time.exception),
            "Extension schedule daily time must look like HH:MM.",
        )
        self.assertEqual(
            str(unknown_zone.exception),
            "Unknown extension schedule timezone 'Mars/Olympus'.",
        )


class EnochScheduleIntervalTests(unittest.TestCase):
    def test_interval_targets_stay_anchored_and_skip_missed_slots(self) -> None:
        anchor = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)

        def target(minute: int) -> datetime:
            return next_interval_run(
                anchor,
                600,
                datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
                + timedelta(minutes=minute),
            )

        self.assertEqual(
            next_interval_run(None, 600, anchor),
            datetime(2026, 6, 30, 12, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(target(5), datetime(2026, 6, 30, 12, 10, tzinfo=timezone.utc))
        self.assertEqual(target(10), datetime(2026, 6, 30, 12, 20, tzinfo=timezone.utc))
        self.assertEqual(target(97), datetime(2026, 6, 30, 13, 40, tzinfo=timezone.utc))

    def test_interval_targets_accept_naive_utc_and_other_zones(self) -> None:
        naive_anchor = datetime(2026, 6, 30, 12, 0)
        local_now = datetime(2026, 6, 30, 5, 5, tzinfo=ZoneInfo(PACIFIC))

        self.assertEqual(
            next_interval_run(naive_anchor, 600, local_now),
            datetime(2026, 6, 30, 12, 10, tzinfo=timezone.utc),
        )
        with self.assertRaises(ScheduleError):
            next_interval_run(naive_anchor, 0, local_now)


class EnochScheduleDaylightSavingTests(unittest.TestCase):
    def test_daily_run_follows_local_wall_clock_time(self) -> None:
        before_pacific_noon = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)

        self.assertEqual(
            next_daily_run("09:00", PACIFIC, before_pacific_noon),
            datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            next_daily_run(
                "09:00",
                PACIFIC,
                datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc),
            ),
            datetime(2026, 8, 1, 16, 0, tzinfo=timezone.utc),
        )

    def test_spring_forward_gap_lands_on_the_first_instant_after_the_jump(self) -> None:
        zone = ZoneInfo(PACIFIC)
        skipped = next_daily_run(
            "02:30",
            PACIFIC,
            datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc),
        )
        narrow_gap = next_daily_run(
            "02:15",
            LORD_HOWE,
            datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc),
        )

        # 2026-03-08 02:30 never happens in the Pacific zone: 02:00 becomes 03:00.
        self.assertEqual(skipped, datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(
            skipped.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z"),
            "2026-03-08 03:00 PDT",
        )
        # Lord Howe jumps by 30 minutes, so 02:15 resolves to 02:30 rather than 03:00.
        self.assertEqual(
            narrow_gap.astimezone(ZoneInfo(LORD_HOWE)).strftime("%Y-%m-%d %H:%M"),
            "2026-10-04 02:30",
        )

    def test_fall_back_repeat_resolves_to_its_first_occurrence(self) -> None:
        repeated = next_daily_run(
            "01:30",
            PACIFIC,
            datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc),
        )

        # 2026-11-01 01:30 happens twice; the earlier daylight-time instant wins.
        self.assertEqual(repeated, datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc))
        self.assertEqual(repeated.astimezone(ZoneInfo(PACIFIC)).fold, 0)

    def test_daily_run_fires_once_per_local_calendar_day(self) -> None:
        zone = ZoneInfo(PACIFIC)
        for daily_time, start in (
            ("02:30", datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc)),
            ("01:30", datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)),
        ):
            with self.subTest(daily_time=daily_time):
                current = start
                occurrences = []
                for _ in range(6):
                    current = next_daily_run(daily_time, PACIFIC, current)
                    occurrences.append(current)

                local_dates = [item.astimezone(zone).date() for item in occurrences]
                self.assertEqual(len(set(local_dates)), len(local_dates))
                self.assertEqual(sorted(local_dates), local_dates)
                self.assertEqual(
                    (local_dates[-1] - local_dates[0]).days,
                    len(local_dates) - 1,
                )
                self.assertEqual(occurrences, sorted(occurrences))


if __name__ == "__main__":
    unittest.main()
