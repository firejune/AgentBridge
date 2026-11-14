"""Claude Code reader of session_usage.py, on committed synthetic transcripts only."""
from contextlib import redirect_stderr, redirect_stdout
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

FIXTURES = Path(__file__).parent / 'fixtures/session-usage/claude'
SID = '00000000-0000-4000-8000-00000000c001'
TWO_MODELS = '00000000-0000-4000-8000-00000000c002'
TOKENS = ('input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens')


def read(case, sid=SID, prices=None):
    with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(FIXTURES/case)}):
        return session_usage.query('claude', sid, prices)


def usage_lines(path):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    return [row['message']['usage'] for row in rows if row.get('type') == 'assistant']


class ClaudeSessionUsageTests(unittest.TestCase):
    def test_mapping_normalises_to_total_prompt_including_cache(self):
        row = read('basic')
        # msg_1: input 3, cache write 100, cache read 1000, output 20 (largest of its block lines)
        # msg_2: input 2, cache write 0, cache read 1100, output 7 (no requestId, two lines)
        # input_tokens = input + cache_creation + cache_read; cached = cache_read; cache write = cache_creation
        self.assertEqual([row[k] for k in TOKENS], [3+100+1000 + 2+0+1100, 1000+1100, 100, 20+7])
        notes = ' '.join(row['notes'])
        self.assertIn('input_tokens = input + cache_creation + cache_read', notes)
        self.assertIn('cache-write surcharge not applied', notes)
        self.assertEqual(row['scope'], 'session-alone')
        self.assertEqual(row['source'], str(FIXTURES/'basic/projects/fixture-project'/f'{SID}.jsonl'))

    def test_duplicate_content_block_lines_are_one_response(self):
        path = FIXTURES/'basic/projects/fixture-project'/f'{SID}.jsonl'
        naive = sum(usage['cache_read_input_tokens'] for usage in usage_lines(path))
        self.assertEqual(naive, 1000*2 + 1100*2)  # the plain sum this reader must not report
        self.assertEqual(read('basic')['cached_input_tokens'], 2100)

    def test_streamed_growth_and_zeroed_rewrite_keep_the_largest(self):
        row = read('basic')
        self.assertEqual(row['output_tokens'], 27)
        self.assertIn('per-counter maximum kept', ' '.join(row['notes']))

    def test_missing_request_id_deduplicates_on_message_id(self):
        path = FIXTURES/'basic/projects/fixture-project'/f'{SID}.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(sum(1 for r in rows if r.get('message', {}).get('id') == 'msg_2' and 'requestId' not in r), 2)
        self.assertEqual(read('basic')['input_tokens'], 1103 + 1102)  # msg_1 once, msg_2 once (not 2 x 1102)

    def test_subagents_reported_apart_without_replayed_parent_response(self):
        row = read('basic')
        # agent-a1 replays parent msg_1 under another requestId (dropped); msg_s1 appears in both files.
        self.assertEqual(row['subagents'], {'input_tokens': 1+10+50 + 1+0+60, 'cached_input_tokens': 110,
                                            'cache_write_tokens': 10, 'output_tokens': 10, 'count': 2})
        self.assertIn('reported separately under subagents', ' '.join(row['notes']))
        self.assertNotIn('subagents not collected', ' '.join(row['notes']))

    def test_no_subagents_is_zero_object_not_null(self):
        row = read('two-models', TWO_MODELS)
        self.assertEqual(row['subagents'], {'input_tokens': 0, 'cached_input_tokens': 0,
                                            'cache_write_tokens': 0, 'output_tokens': 0, 'count': 0})

    def test_two_models_take_last_and_withhold_usd(self):
        row = read('two-models', TWO_MODELS, FIXTURES/'prices.md')
        self.assertEqual(row['model'], 'second-fixture-model')
        self.assertIsNone(row['usd'])
        self.assertIn('multiple models', ' '.join(row['notes']))

    def test_synthetic_model_ignored_and_single_model_priced(self):
        row = read('basic', prices=FIXTURES/'prices.md')
        self.assertEqual(row['model'], 'fixture-model')
        self.assertIn('placeholder model values ignored: <synthetic>', ' '.join(row['notes']))
        self.assertNotIn('multiple models', ' '.join(row['notes']))
        self.assertAlmostEqual(row['usd'], ((2205-2100)*2 + 2100*0.5 + 27*10) / 1_000_000)

    def test_durations_in_time_order(self):
        row = read('basic')
        # An attachment written after later records carries the earliest instant.
        self.assertEqual((row['first_ts'], row['last_ts']), ('2026-01-01T09:59:00.000Z', '2026-01-01T10:20:12.000Z'))
        self.assertEqual(row['wall_seconds'], 1272)
        self.assertEqual(row['active_seconds'], 60+5+1+4+1+1)  # the 1200 s gap is idle
        self.assertIn('not in file order', ' '.join(row['notes']))

    def test_missing_transcript_names_the_path(self):
        missing = '00000000-0000-4000-8000-00000000c0ff'
        with self.assertRaisesRegex(ValueError, f'projects/\\*/{missing}.jsonl: no Claude transcript found'):
            read('basic', missing)
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(FIXTURES/'basic')}), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertNotEqual(bridge.main(['session-usage', '--provider', 'claude', '--id', missing]), 0)
        self.assertIn(f'{missing}.jsonl', out.getvalue() + err.getvalue())

    def test_malformed_line_and_bad_id_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'jsonl: record 1'):
            read('invalid')
        with self.assertRaisesRegex(ValueError, 'not a UUID'):
            read('basic', '../escape')


if __name__ == '__main__':
    unittest.main()
