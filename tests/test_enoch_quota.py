from datetime import datetime, timezone
import json
from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from enoch.commands import help_message, runtime_command_reference
from enoch.providers.codex_quota import _read_limits, parse_limits, read_quota
from enoch.quota import format_quota, quota_command


class QuotaTests(unittest.TestCase):
    def test_prefers_all_named_buckets_and_uses_actual_window_duration(self):
        result = parse_limits({
            'rateLimits': {'primary': {'usedPercent': 99}},
            'rateLimitsByLimitId': {
                'codex': {'planType': 'plus', 'primary': {
                    'usedPercent': 25, 'windowDurationMins': 10080, 'resetsAt': 1900000000}},
                'other': {'primary': {'usedPercent': None, 'windowDurationMins': 300},
                          'secondary': None},
            }, 'accountId': 'private-account-id', 'rateLimitResetCredits': {'private': 'credit'},
        })
        self.assertEqual([w['label'] for w in result['windows']], ['codex / 7d', 'other / 5h'])
        self.assertEqual([w['used_percent'] for w in result['windows']], [25, None])
        self.assertNotIn('private', json.dumps(result))

    def test_legacy_and_null_limits(self):
        self.assertEqual(parse_limits({'rateLimits': None})['windows'], [])
        result = parse_limits({'rateLimits': {'primary': {'usedPercent': 0, 'resetsAt': None}}})
        self.assertEqual(result['windows'][0]['used_percent'], 0)
        self.assertIsNone(result['windows'][0]['resets_at'])

    def test_missing_data_is_unknown_and_percentages_are_not_tokens(self):
        result = format_quota('GPT', {'windows': [
            {'label': 'unknown', 'used_percent': None},
            {'label': 'zero', 'used_percent': 0},
            {'label': 'over', 'used_percent': 120},
            {'label': 'invalid', 'used_percent': float('nan')},
        ]})
        self.assertIn('unknown: usage unknown; remaining unknown', result)
        self.assertIn('zero: 100% remaining (0% used)', result)
        self.assertIn('over: 0% remaining (120% used)', result)
        self.assertIn('reset unknown / not reported', result)
        self.assertNotIn('nan%', result)

    def test_reset_countdown_past_and_invalid_timestamps(self):
        now = datetime(2026, 9, 13, tzinfo=timezone.utc)
        result = format_quota('Claude', {'source': 'live', 'windows': [
            {'label': '5h', 'resets_at': '2026-09-13T02:30:00Z'},
            {'label': '7d', 'resets_at': now.timestamp() - 1},
            {'label': 'bad', 'resets_at': 1e100},
        ]}, now=now)
        self.assertIn('in 2h 30m', result)
        self.assertIn('reset time passed; query again to confirm', result)
        self.assertIn('reset unknown / not reported', result)
        self.assertIn('checked ', result)

    def test_remote_labels_cannot_become_slack_mentions(self):
        result = format_quota('Claude', {'windows': [{'label': '<!channel>\n`oops`'}]})
        self.assertNotIn('<!channel>', result)
        self.assertNotIn('`', result)

    def test_default_skips_missing_cli_and_uses_injected_runtime(self):
        runtime = SimpleNamespace(name='codex', quota=Mock(return_value={'windows': [
            {'label': '5h', 'used_percent': 20}]}))
        missing = SimpleNamespace(quota=Mock(return_value=None))
        with patch('enoch.quota.available_providers', return_value=('codex', 'claude')), \
             patch('enoch.quota.load_provider', return_value=missing) as load:
            result = quota_command('', Path('/tmp'), runtime=runtime)
        self.assertIn('80% remaining', result)
        self.assertNotIn('Claude', result)
        load.assert_called_once_with('runtime', Path('/tmp'), name='claude')
        runtime.quota.assert_called_once_with(Path('/tmp'))

    def test_gpt_alias_queries_only_codex_and_does_not_switch_runtime(self):
        runtime = SimpleNamespace(name='claude', quota=Mock())
        codex = SimpleNamespace(quota=Mock(return_value={'windows': []}))
        with patch('enoch.quota.available_providers', return_value=('codex', 'claude')), \
             patch('enoch.quota.load_provider', return_value=codex) as load:
            result = quota_command(' GPT ', Path('/tmp'), runtime=runtime)
        load.assert_called_once_with('runtime', Path('/tmp'), name='codex')
        runtime.quota.assert_not_called()
        self.assertIn('GPT / Codex', result)

    def test_legacy_function_runtime_uses_registered_codex_quota_reader(self):
        runtime = SimpleNamespace(name='codex')
        codex = SimpleNamespace(quota=Mock(return_value={'windows': []}))
        with patch('enoch.quota.available_providers', return_value=('codex',)), \
             patch('enoch.quota.load_provider', return_value=codex) as load:
            result = quota_command('', Path('/tmp'), runtime=runtime)
        load.assert_called_once_with('runtime', Path('/tmp'), name='codex')
        self.assertIn('GPT / Codex', result)

    def test_absent_providers_and_invalid_arguments_do_not_launch_anything(self):
        with patch('enoch.quota.available_providers', return_value=()), \
             patch('enoch.quota.load_provider') as load:
            self.assertIn('No quota-capable', quota_command('', Path('/tmp')))
            self.assertIn('No quota-capable', quota_command('claude', Path('/tmp')))
            self.assertIn('.quota [', quota_command('--reset', Path('/tmp'), prefix='.'))
        load.assert_not_called()

    def test_provider_failure_does_not_hide_other_provider_or_leak_exception(self):
        broken = SimpleNamespace(name='claude', quota=Mock(side_effect=RuntimeError('secret-token')))
        good = SimpleNamespace(quota=Mock(return_value={'windows': [{'used_percent': 25}]}))
        with patch('enoch.quota.available_providers', return_value=('codex', 'claude')), \
             patch('enoch.quota.load_provider', return_value=good):
            result = quota_command('', Path('/tmp'), runtime=broken)
        self.assertIn('75% remaining', result)
        self.assertIn('quota query failed', result)
        self.assertNotIn('secret', result)

    def test_registry_exposes_quota_in_help_and_agent_command_reference(self):
        self.assertIn('.quota [', help_message(command_prefix='.'))
        self.assertIn('GPT is an alias', help_message('quota'))
        self.assertIn('!quota [', runtime_command_reference(command_prefix='!'))

    def test_missing_codex_skips_launch(self):
        with patch('enoch.brain.resolve_codex_executable', return_value=SimpleNamespace(path=None)), \
             patch('enoch.providers.codex_quota.subprocess.Popen') as process:
            self.assertIsNone(read_quota())
        process.assert_not_called()

    def test_codex_protocol_initializes_reads_and_never_starts_a_turn(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = self.fake_codex(root, '''
first = json.loads(sys.stdin.readline())
assert first['method'] == 'initialize'
print(json.dumps({'id': first['id'], 'result': {}}), flush=True)
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
request = json.loads(sys.stdin.readline())
assert request['method'] == 'account/rateLimits/read'
print(json.dumps({'method': 'notification'}), flush=True)
print(json.dumps({'id': request['id'], 'result': {'rateLimits': None}}), flush=True)
time.sleep(60)
''')
            self.assertEqual(_read_limits(str(executable), root, 3), {'rateLimits': None})
            pid = int((root / 'pid').read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_codex_timeout_stops_child_and_errors_do_not_leak(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = self.fake_codex(root, 'time.sleep(60)')
            with self.assertRaises(TimeoutError):
                _read_limits(str(executable), root, 0.3)
            with self.assertRaises(ProcessLookupError):
                os.kill(int((root / 'pid').read_text()), 0)
            with patch('enoch.brain.resolve_codex_executable', return_value=SimpleNamespace(path=str(executable))), \
                 patch('enoch.providers.codex_quota._read_limits', side_effect=ValueError('secret-token')):
                result = read_quota(root)
            self.assertNotIn('secret-token', str(result))

    def fake_codex(self, root, body):
        executable = root / 'codex'
        executable.write_text(f'#!{sys.executable}\nimport sys,json,time,os\nfrom pathlib import Path\n'
                              'Path("pid").write_text(str(os.getpid()))\n' + body)
        executable.chmod(0o755)
        return executable
