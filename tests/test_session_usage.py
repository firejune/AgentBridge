"""Fixtures for session_usage.py, bridge.py and sessions.py offline lookup."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import session_usage
import sessions

FIXTURES = Path(__file__).parent / 'fixtures/session-usage'
SID = '00000000-0000-4000-8000-000000000001'


class SessionUsageTests(unittest.TestCase):
    def read(self, fixture='codex.jsonl', prices=None):
        with patch.object(sessions, 'read_codex', return_value={'session': {'path': str(FIXTURES/fixture)}}):
            return session_usage.query('codex', SID, prices)

    def test_final_cumulative_not_sum_including_compaction_and_null_info(self):
        row = self.read()
        self.assertEqual([row[k] for k in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'cache_write_tokens')], [150, 60, 20, 0])
        self.assertEqual(row['wall_seconds'], 1221)
        self.assertEqual(row['active_seconds'], 21)
        self.assertEqual(row['model'], 'fixture-model')
        self.assertIn('reasoning', ' '.join(row['notes']))

    def test_explicit_prices(self):
        row = self.read(prices=FIXTURES/'prices.md')
        self.assertAlmostEqual(row['usd'], .00041)
        self.assertIsNone(self.read()['usd'])

    def test_missing_fields_remain_null(self):
        row = self.read('missing.jsonl', FIXTURES/'prices.md')
        for field in session_usage.FIELDS:
            if field != 'source':
                self.assertIsNone(row[field], field)
        self.assertIn('no price row', ' '.join(row['notes']))

    def test_fork_and_subagent_subtract_recorded_inherited_prefix(self):
        for fixture in ('fork-ordinal.jsonl', 'subagent-ordinal.jsonl'):
            row = self.read(fixture)
            self.assertEqual([row[k] for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')], [50, 20, 10])
            self.assertIn('ordinal boundary', ' '.join(row['notes']))
            self.assertEqual(row['scope'], 'session-alone')
            self.assertIsNone(row['subagents'])

    def test_fork_exact_parent_snapshot_prefix(self):
        def lookup(sid, **kwargs):
            name = 'fork-parent.jsonl' if sid == SID else 'parent.jsonl'
            return {'session': {'path': str(FIXTURES/name)}}
        with patch.object(sessions, 'read_codex', side_effect=lookup):
            row = session_usage.query('codex', SID)
        self.assertEqual(row['input_tokens'], 50)
        self.assertEqual(row['output_tokens'], 10)
        self.assertIn('exact parent snapshot match', ' '.join(row['notes']))

    def test_unverifiable_fork_baseline_is_null_not_guessed(self):
        def lookup(sid, **kwargs):
            if sid != SID:
                raise ValueError('parent fixture unavailable')
            return {'session': {'path': str(FIXTURES/'fork-unknown.jsonl')}}
        with patch.object(sessions, 'read_codex', side_effect=lookup):
            row = session_usage.query('codex', SID)
        self.assertIsNone(row['input_tokens'])
        self.assertIn('baseline cannot be determined', ' '.join(row['notes']))

    def test_mixed_models_do_not_attribute_all_cost_to_last_model(self):
        row = self.read('mixed.jsonl', FIXTURES/'prices.md')
        self.assertEqual(row['model'], 'second-fixture-model')
        self.assertIsNone(row['usd'])
        self.assertIn('multiple models', ' '.join(row['notes']))

    def test_record_identity_mismatch_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'session identity mismatch'):
            self.read('wrong-id.jsonl')

    def test_out_of_order_time_does_not_invent_duration(self):
        row = self.read('unordered.jsonl')
        self.assertIsNone(row['wall_seconds'])
        self.assertIsNone(row['active_seconds'])
        self.assertIn('out of order', ' '.join(row['notes']))

    def test_nonfinite_price_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'invalid price'):
            self.read(prices=FIXTURES/'bad-prices.md')

    def test_malformed_record_fails_with_path(self):
        with self.assertRaisesRegex(ValueError, 'invalid.jsonl: record 1'):
            self.read('invalid.jsonl')

    def test_unreadable_record_fails(self):
        with self.assertRaises(OSError):
            self.read('absent.jsonl')

    def test_reader_dispatch_accepts_independent_provider_functions(self):
        for provider in ('claude', 'agy'):
            expected = session_usage.empty(provider, SID)
            with patch.object(session_usage, 'read_' + provider, return_value=expected) as reader:
                row = session_usage.query(provider, SID)
            reader.assert_called_once_with(SID)
            self.assertEqual(row['provider'], provider)

    def test_cli_route_uses_existing_exact_lookup(self):
        route = {'name': 'fixture-runner', 'transport': 'codex', 'session': SID}
        with patch.object(bridge.broker, 'request', return_value=[route]) as request, patch.object(sessions, 'read_codex', return_value={'session': {'path': str(FIXTURES/'codex.jsonl')}}), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bridge.main(['session-usage', 'fixture-runner', '--project', 'fixture-project', '--json']), 0)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], {'op':'routes', 'project':'fixture-project'})
        self.assertEqual(json.loads(out.getvalue())['input_tokens'], 150)

    def test_cli_explicit_id_does_not_contact_broker(self):
        with patch.object(session_usage, 'read_agy', return_value=session_usage.empty('agy', SID)), patch.object(bridge.broker, 'request', side_effect=AssertionError('broker called')), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bridge.main(['session-usage', '--provider', 'agy', '--id', SID]), 0)
        self.assertEqual(len(out.getvalue().splitlines()), 1)

    def test_cli_rejects_ambiguous_target(self):
        for argv in ([], ['--provider','codex'], ['fixture-runner','--project','fixture-project','--id',SID]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(bridge.main(['session-usage', *argv]), 1)

    def test_offline_lookup_reuses_session_interface_without_process(self):
        # Mock the directory listing, while every opened record remains a committed fixture.
        with patch.dict(os.environ, {'CODEX_HOME': str(FIXTURES)}), patch.object(Path, 'glob', side_effect=[[FIXTURES/'codex.jsonl'], []]), patch.object(sessions, 'CodexClient', side_effect=AssertionError('provider process started')):
            self.assertEqual(sessions.read_codex(SID, mode='record')['session']['path'], str(FIXTURES/'codex.jsonl'))

    def test_offline_lookup_missing_and_duplicate_fail(self):
        for listings in ([[], []], [[FIXTURES/'codex.jsonl'], [FIXTURES/'codex.jsonl']]):
            with patch.dict(os.environ, {'CODEX_HOME': str(FIXTURES)}), patch.object(Path, 'glob', side_effect=listings):
                with self.assertRaisesRegex(ValueError, 'Expected one Codex record at'):
                    sessions.read_codex(SID, mode='record')

    def test_invalid_id_never_looks_up_paths(self):
        with patch.object(Path, 'glob', side_effect=AssertionError('lookup called')):
            with self.assertRaises(ValueError):
                sessions.read_codex('../record', mode='record')


if __name__ == '__main__':
    unittest.main()
