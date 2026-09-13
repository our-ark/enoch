from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from enoch.app.core import EnochApplication
from enoch.app.epoch import StaleDaemonEpoch, begin_daemon_epoch
from enoch.app.notifications import NotificationDeliveryService, NotificationResult, notification_records
from enoch.config import write_section_value
from enoch.identity import load_identity
from enoch.quota_warnings import (
    QuotaWarningMonitor, prepare_warnings, record_warning_result, warning_settings, warning_state_path,
)
from enoch.state import StateCorruptionError


NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


def snapshot(remaining=10, *, label='codex / 5h', reset=300, error=None):
    return {'source': 'test account quota', 'plan': 'plus', 'error': error, 'windows': [{
        'label': label, 'used_percent': None if remaining is None else 100 - remaining,
        'resets_at': (NOW + timedelta(minutes=reset)).isoformat() if reset is not None else None,
    }]}


class Chat:
    name = 'test-chat'
    provider_kind = 'chat'
    allowed_conversation_id = 'owner'
    command_prefix = '.'

    def __init__(self):
        self.sent = []
        self.failures = 0

    def receive(self, cursor=None): return ()
    def edit_message(self, *args): pass
    def send_read_ack(self, *args): pass
    def send_message(self, conversation_id, text):
        if self.failures:
            self.failures -= 1
            raise OSError('temporary send failure')
        self.sent.append((conversation_id, text))
        return str(len(self.sent))


class QuotaWarningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.chat = Chat()
        self.app = EnochApplication(load_identity(), self.root, self.chat)
        self.sample = snapshot(12)
        self.collect = Mock(side_effect=lambda: iter([('codex', self.sample)]))
        self.app.quota_warnings.collect = self.collect

    def poll(self, remaining=None, **kwargs):
        if remaining is not None:
            self.sample = snapshot(remaining, **kwargs)
        return self.app.quota_warnings.check_once(now=NOW)

    def test_threshold_crossings_send_once_with_reset_and_remaining(self):
        for balance, expected in [(12, 0), (10, 1), (10, 0), (9, 0), (5, 1), (2, 0), (1, 1), (0, 0)]:
            with self.subTest(balance=balance):
                self.assertEqual(self.poll(balance), expected)
        self.assertEqual(len(self.chat.sent), 3)
        for threshold, (destination, text) in zip((10, 5, 1), self.chat.sent):
            self.assertEqual(destination, 'owner')
            self.assertIn(f'≤{threshold}% remaining', text)
            self.assertIn(f'{threshold}% remaining', text)
            self.assertIn('resets ', text)
            self.assertIn('in 5h', text)
            self.assertIn('checked ', text)

    def test_jump_and_initial_low_balance_coalesce_to_most_urgent_tier(self):
        self.poll(12)
        self.assertEqual(self.poll(4), 1)
        self.assertIn('≤5% remaining', self.chat.sent[-1][1])
        self.assertEqual(self.poll(0), 1)
        self.assertIn('≤1% remaining', self.chat.sent[-1][1])
        self.assertEqual(len(self.chat.sent), 2)

    def test_restart_preserves_alert_history(self):
        self.poll(10)
        restarted = EnochApplication(load_identity(), self.root, self.chat)
        restarted.quota_warnings.collect = self.collect
        self.assertEqual(restarted.quota_warnings.check_once(now=NOW), 0)
        self.sample = snapshot(5)
        self.assertEqual(restarted.quota_warnings.check_once(now=NOW), 1)
        self.assertEqual(len(self.chat.sent), 2)

    def test_window_reset_rearms_but_jitter_and_usage_bounce_do_not(self):
        self.poll(5)
        self.assertEqual(self.poll(9), 0)
        self.assertEqual(self.poll(5, reset=300 + 0.5 / 60), 0)
        self.assertEqual(self.poll(5, reset=200), 0)
        self.assertEqual(self.poll(15), 0)
        self.assertEqual(self.poll(5), 0)
        self.assertEqual(self.poll(5, reset=400), 1)
        self.assertEqual(len(self.chat.sent), 2)

    def test_missing_or_expired_data_and_provider_errors_do_not_warn(self):
        for sample in [snapshot(None), snapshot(5, reset=-1), snapshot(5, error='unavailable'),
                       {'windows': None}, {'windows': [{'label': 'bad', 'used_percent': float('nan')}]},
                       {'windows': [{'label': 'bad', 'used_percent': -5}]}]:
            self.sample = sample
            self.assertEqual(self.poll(), 0)
        self.assertEqual(self.chat.sent, [])

    def test_unknown_reset_is_explicit_and_newly_reported_reset_does_not_duplicate(self):
        self.assertEqual(self.poll(9, reset=None), 1)
        self.assertIn('reset unknown / not reported', self.chat.sent[-1][1])
        self.assertEqual(self.poll(9), 0)

    def test_unknown_reset_rearms_after_observed_quota_recovery(self):
        self.poll(9, reset=None)
        self.poll(15, reset=None)
        self.assertEqual(self.poll(9, reset=None), 1)

    def test_missing_new_reset_requires_recovery_after_old_window_expires(self):
        self.poll(9)
        later = NOW + timedelta(hours=6)
        self.sample = snapshot(9, reset=None)
        self.assertEqual(self.app.quota_warnings.check_once(now=later), 0)
        self.sample = snapshot(50, reset=None)
        self.assertEqual(self.app.quota_warnings.check_once(now=later), 0)
        self.sample = snapshot(9, reset=None)
        self.assertEqual(self.app.quota_warnings.check_once(now=later), 1)

    def test_windows_and_providers_are_independent_and_duplicate_rows_are_coalesced(self):
        a = snapshot(5)
        a['windows'].extend([snapshot(10, label='codex / 7d')['windows'][0], a['windows'][0]])
        self.app.quota_warnings.collect = lambda: iter([('codex', a), ('claude', snapshot(1))])
        self.assertEqual(self.poll(), 3)
        self.assertEqual(self.poll(), 0)

    def test_delivery_failure_retries_same_immutable_intent(self):
        self.chat.failures = 1
        self.assertEqual(self.poll(10), 0)
        original = next(iter(json.loads(warning_state_path(self.root).read_text())['windows'].values()))['pending']
        self.assertEqual(self.poll(9), 1)
        self.assertEqual(self.chat.sent[0][1], original['text'])
        self.assertEqual(self.poll(9), 0)
        self.assertEqual(len(notification_records('test-chat', self.root)), 1)

    def test_more_urgent_level_supersedes_failed_warning(self):
        self.chat.failures = 1
        self.poll(10)
        self.assertEqual(self.poll(4), 1)
        self.assertIn('≤5% remaining', self.chat.sent[0][1])
        self.assertEqual(self.poll(8), 0)

    def test_expired_pending_warning_is_not_recovered_on_startup(self):
        self.chat.failures = 1
        self.poll(10)
        restarted = EnochApplication(load_identity(), self.root, self.chat)
        restarted.quota_warnings.collect = lambda: iter([('codex', snapshot(50, reset=400))])
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(restarted.quota_warnings.check_once(now=NOW), 0)
        self.assertEqual(self.chat.sent, [])

    def test_delivered_receipt_closes_crash_gap_even_when_balance_recovers(self):
        with patch('enoch.quota_warnings.record_warning_result', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                self.poll(1)
        restarted = EnochApplication(load_identity(), self.root, self.chat)
        restarted.quota_warnings.collect = self.collect
        self.sample = snapshot(9)
        self.assertEqual(restarted.quota_warnings.check_once(now=NOW), 0)
        self.sample = snapshot(1)
        self.assertEqual(restarted.quota_warnings.check_once(now=NOW), 0)
        self.assertEqual(len(self.chat.sent), 1)

    def test_terminal_delivery_is_not_retried_forever(self):
        self.app.quota_warnings.deliver = Mock(return_value=NotificationResult(delivered=False, terminal=True))
        self.poll(10)
        self.poll(9)
        self.app.quota_warnings.deliver.assert_called_once()
        self.poll(5)
        self.assertEqual(self.app.quota_warnings.deliver.call_count, 2)

    def test_disabled_missing_destination_and_stopped_monitor_do_not_query(self):
        write_section_value('quota', 'warnings_enabled', 'false', self.root)
        self.poll(1)
        write_section_value('quota', 'warnings_enabled', 'true', self.root)
        self.chat.allowed_conversation_id = None
        self.poll(1)
        self.chat.allowed_conversation_id = 'owner'
        self.app.quota_warnings.stop()
        self.poll(1)
        self.collect.assert_not_called()

    def test_stale_daemon_or_changed_destination_during_query_cannot_warn(self):
        def stale():
            begin_daemon_epoch(self.root)
            yield 'codex', snapshot(1)
        self.app.quota_warnings.collect = stale
        with self.assertRaises(StaleDaemonEpoch):
            self.poll()
        self.assertEqual(self.chat.sent, [])
        self.assertFalse(warning_state_path(self.root).exists())

    def test_shutdown_during_query_discards_late_result(self):
        def stopped():
            self.app.quota_warnings.stop()
            yield 'codex', snapshot(1)
        self.app.quota_warnings.collect = stopped
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.chat.sent, [])

    def test_destination_change_during_query_discards_result(self):
        def changed():
            self.chat.allowed_conversation_id = 'different-owner'
            yield 'codex', snapshot(1)
        self.app.quota_warnings.collect = changed
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.chat.sent, [])

    def test_disabling_while_query_is_in_flight_discards_result(self):
        def disabled():
            write_section_value('quota', 'warnings_enabled', 'false', self.root)
            yield 'codex', snapshot(1)
        self.app.quota_warnings.collect = disabled
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.chat.sent, [])

    def test_corrupt_state_is_preserved_without_sending(self):
        path = warning_state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken')
        with self.assertRaises(StateCorruptionError):
            self.poll(1)
        self.assertEqual(path.read_text(), '{broken')
        self.assertEqual(self.chat.sent, [])

    def test_worker_runs_independently_and_stops_waiting_promptly(self):
        observed = threading.Event()
        monitor = self.app.quota_warnings
        monitor.check_once = Mock(side_effect=observed.set)
        monitor.start()
        monitor.start()
        self.assertTrue(observed.wait(timeout=1))
        monitor.stop(timeout_seconds=1)
        self.assertFalse(monitor._thread.is_alive())
        monitor.check_once.assert_called_once()

    def test_poll_interval_has_safe_bounds(self):
        self.assertEqual(warning_settings(self.root).poll_interval_seconds, 60)
        write_section_value('quota', 'poll_interval_seconds', '300', self.root)
        self.assertEqual(warning_settings(self.root).poll_interval_seconds, 300)
        for invalid in ('0', 'nonsense', '3601'):
            write_section_value('quota', 'poll_interval_seconds', invalid, self.root)
            self.assertEqual(warning_settings(self.root).poll_interval_seconds, 60)

    def test_notification_recovery_filter_preserves_regular_retries(self):
        self.chat.failures = 1
        self.app.notifications.send('owner', 'ordinary message', idempotency_key='ordinary')
        self.app.notifications.recover(exclude_key_prefixes=('quota-warning:',))
        self.assertEqual(self.chat.sent, [('owner', 'ordinary message')])
