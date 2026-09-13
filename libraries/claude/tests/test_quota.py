import json
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'libraries/provider-kit/src'))
sys.path.insert(0, str(ROOT / 'libraries/claude/src'))
from our_ark_claude import ClaudeRuntime
from our_ark_claude.quota import parse_usage, read_quota


class ClaudeQuotaTests(unittest.TestCase):
    def test_window_mapping_nulls_and_model_scopes(self):
        snapshot = parse_usage({'rate_limits_available': True, 'subscription_type': 'pro',
                               'session': {'total_cost_usd': 999}, 'rate_limits': {
            'five_hour': {'utilization': 0, 'resets_at': None},
            'seven_day': {'utilization': 3, 'resets_at': '2026-09-17T03:59:59Z'},
            'seven_day_opus': None,
            'model_scoped': [{'display_name': 'Fable', 'utilization': 12}],
        }})
        self.assertEqual([w['used_percent'] for w in snapshot['windows']], [0, 3, 12])
        self.assertIsNone(snapshot['windows'][0]['resets_at'])
        self.assertEqual(snapshot['windows'][2]['label'], '7d / Fable')
        self.assertNotIn('session', snapshot)

    def test_non_subscription_and_null_limits_are_unavailable(self):
        self.assertIn('authentication mode', parse_usage({'rate_limits_available': False})['error'])
        self.assertEqual(parse_usage({'rate_limits': None})['windows'], [])

    def test_uses_only_control_requests_no_model_prompt_or_tools(self):
        response = {'type': 'control_response', 'response': {'request_id': 'quota-read',
                    'subtype': 'success', 'response': {'rate_limits': {'five_hour': {'utilization': 25}}}}}
        with patch('our_ark_claude.quota.subprocess.run', return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(response))) as run:
            snapshot = read_quota('/bin/claude', Path('/tmp'))
        self.assertEqual(snapshot['windows'][0]['used_percent'], 25)
        args, kwargs = run.call_args
        requests = [json.loads(line) for line in kwargs['input'].splitlines()]
        self.assertEqual([r['request']['subtype'] for r in requests], ['initialize', 'get_usage'])
        self.assertTrue(all(r['type'] == 'control_request' for r in requests))
        command = args[0]
        self.assertEqual(command[command.index('--tools') + 1], '')
        self.assertIn('{"disableAllHooks":true}', command)
        self.assertIn('--no-session-persistence', command)
        self.assertIn('{"mcpServers":{}}', command)

    def test_failure_timeout_and_unsupported_cli_are_safe(self):
        cases = [SimpleNamespace(returncode=1, stdout='secret-token'),
                 SimpleNamespace(returncode=0, stdout='not json'),
                 SimpleNamespace(returncode=0, stdout=json.dumps({'type': 'control_response',
                    'response': {'request_id': 'quota-read', 'subtype': 'error', 'error': 'secret-token'}}))]
        for case in cases:
            with patch('our_ark_claude.quota.subprocess.run', return_value=case):
                result = read_quota('/bin/claude')
            self.assertIn('error', result)
            self.assertNotIn('secret-token', str(result))
        with patch('our_ark_claude.quota.subprocess.run', side_effect=subprocess.TimeoutExpired('claude', 20)):
            self.assertIn('timed out', read_quota('/bin/claude')['error'])

    def test_missing_cli_skips_query(self):
        runtime = ClaudeRuntime()
        with patch.object(runtime, 'resolve_executable', return_value=SimpleNamespace(path=None)), \
             patch('our_ark_claude.quota.read_quota') as query:
            self.assertIsNone(runtime.quota())
        query.assert_not_called()
