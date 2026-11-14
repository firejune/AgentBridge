"""Synthetic protobuf/SQLite fixture tests for the Antigravity session_usage reader."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import session_usage

SID = '00000000-0000-4000-8000-0000000000a1'


def pb_varint(value):
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def pb_tag(field_num, wire_type):
    return pb_varint((field_num << 3) | wire_type)


def pb_uint(field_num, value):
    return pb_tag(field_num, 0) + pb_varint(value)


def pb_bytes(field_num, data):
    raw = data.encode('utf-8') if isinstance(data, str) else bytes(data)
    return pb_tag(field_num, 2) + pb_varint(len(raw)) + raw


def pb_fixed64(field_num, value):
    return pb_tag(field_num, 1) + struct.pack('<Q', value)


def pb_fixed32(field_num, value):
    return pb_tag(field_num, 5) + struct.pack('<I', value)


def make_gen_row(inp=0, out=0, cache_write=0, cache_read=0, thinking=0, response=0,
                 rid=b'resp-1', model='fixture-agy-model', model_enum=1418, extra_wire=False):
    usage = bytearray(pb_uint(1, model_enum))
    for field_num, val in ((2, inp), (3, out), (4, cache_write), (5, cache_read), (9, thinking), (10, response)):
        if val:
            usage += pb_uint(field_num, val)
    if rid is not None:
        usage += pb_bytes(11, rid)
    if extra_wire:
        usage += pb_fixed64(12, 42) + pb_fixed32(13, 7)
    cm = bytearray(pb_uint(3, model_enum) + pb_bytes(4, usage))
    if model is not None:
        cm += pb_bytes(19, model)
    return pb_bytes(1, cm)


class SessionUsageAgyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conv_dir = self.home / 'conversations'
        self.conv_dir.mkdir(parents=True)
        self.db_path = self.conv_dir / f'{SID}.db'

    def write_db(self, rows, create_table=True):
        with closing(sqlite3.connect(self.db_path)) as conn:
            if create_table:
                conn.execute('CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER NOT NULL DEFAULT 0)')
                for idx, blob in enumerate(rows):
                    conn.execute('INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)', (idx, blob, len(blob)))
            else:
                conn.execute('CREATE TABLE trajectory_meta (trajectory_id TEXT PRIMARY KEY)')
            conn.commit()

    def query(self, prices=None, sid=SID):
        with patch.dict(os.environ, {'AGY_HOME': str(self.home)}):
            return session_usage.query('agy', sid, prices)

    def test_normal_normalises_cached_and_cache_write_into_input_tokens(self):
        # Row 0: input(#2)=100, cache_write(#4)=30, cache_read(#5)=200, thinking(#9)=15, response(#10)=25 -> out(#3)=40
        # Row 1: input(#2)=50,  cache_write(#4)=10, cache_read(#5)=80,  thinking(#9)=5,  response(#10)=15 -> out(#3)=20
        self.write_db([
            make_gen_row(inp=100, out=40, cache_write=30, cache_read=200, thinking=15, response=25, rid=b'r0', extra_wire=True),
            make_gen_row(inp=50, out=20, cache_write=10, cache_read=80, thinking=5, response=15, rid=b'r1'),
        ])
        price_file = self.home / 'prices.md'
        price_file.write_text(
            '| model id | input | cached input | output |\n'
            '| --- | --- | --- | --- |\n'
            '| fixture-agy-model | $2.00 | $0.50 | $10.00 |\n',
            encoding='utf-8',
        )
        row = self.query(prices=price_file)
        # Normalised mapping:
        # input_tokens = (#2 + #5 + #4) = (100+50) + (200+80) + (30+10) = 470
        # cached_input_tokens = #5 = 280
        # cache_write_tokens = #4 = 40
        # output_tokens = #3 = 60 (where #9 + #10 = 20 + 40 = 60)
        self.assertEqual(row['provider'], 'agy')
        self.assertEqual(row['id'], SID)
        self.assertEqual(row['model'], 'fixture-agy-model')
        self.assertEqual(row['input_tokens'], 470)
        self.assertEqual(row['cached_input_tokens'], 280)
        self.assertEqual(row['cache_write_tokens'], 40)
        self.assertEqual(row['output_tokens'], 60)
        self.assertEqual(row['source'], str(self.db_path))
        for ts_field in ('first_ts', 'last_ts', 'wall_seconds', 'active_seconds'):
            self.assertIsNone(row[ts_field], ts_field)
        # usd = ((470 - 280) * 2.0 + 280 * 0.5 + 60 * 10.0) / 1_000_000 = (380 + 140 + 600) / 1_000_000 = 0.00112
        self.assertAlmostEqual(row['usd'], 0.00112)
        notes = ' '.join(row['notes'])
        self.assertIn('input_tokens is #2 + #5 + #4', notes)
        self.assertIn('output_tokens is #3 (#9 thinking + #10 response)', notes)
        self.assertIn('retry usage', notes)
        self.assertIn('timestamps not recorded in gen_metadata', notes)

    def test_duplicate_response_id_is_deduplicated_and_keeps_per_counter_max(self):
        self.write_db([
            make_gen_row(inp=100, out=30, cache_write=0, cache_read=50, thinking=10, response=20, rid=b'dup-id'),
            make_gen_row(inp=100, out=30, cache_write=0, cache_read=50, thinking=10, response=20, rid=b'dup-id'),
            make_gen_row(inp=40, out=15, cache_write=5, cache_read=10, thinking=5, response=10, rid=b'second-id'),
        ])
        row = self.query()
        self.assertEqual(row['input_tokens'], (100 + 50 + 0) + (40 + 10 + 5))
        self.assertEqual(row['cached_input_tokens'], 60)
        self.assertEqual(row['cache_write_tokens'], 5)
        self.assertEqual(row['output_tokens'], 45)
        self.assertNotIn('differing counters', ' '.join(row['notes']))

        # If duplicate response IDs carry differing counters, keep the per-counter maximum and note it.
        self.db_path.unlink()
        self.write_db([
            make_gen_row(inp=100, out=20, cache_write=5, cache_read=50, thinking=10, response=10, rid=b'grow-id'),
            make_gen_row(inp=100, out=35, cache_write=0, cache_read=50, thinking=10, response=25, rid=b'grow-id'),
        ])
        diff_row = self.query()
        self.assertEqual(diff_row['input_tokens'], 100 + 50 + 5)
        self.assertEqual(diff_row['cached_input_tokens'], 50)
        self.assertEqual(diff_row['cache_write_tokens'], 5)
        self.assertEqual(diff_row['output_tokens'], 35)
        self.assertIn('duplicate response ids had differing counters; per-counter maximum kept', ' '.join(diff_row['notes']))

    def test_all_zero_row_skipped_and_only_zero_rows_stay_null(self):
        self.write_db([
            make_gen_row(rid=None, model=None),
            make_gen_row(rid=None, model=None),
            make_gen_row(rid=b'r-valid', model=None),
            make_gen_row(inp=80, out=25, cache_write=0, cache_read=20, thinking=10, response=15, rid=b'r-valid'),
        ])
        row = self.query()
        self.assertEqual([row[k] for k in ('input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens')],
                         [100, 20, 0, 25])

        # A database with only all-zero rows yields null token counts, never 0.
        self.db_path.unlink()
        self.write_db([make_gen_row(rid=None, model='fixture-agy-model')])
        empty_row = self.query()
        for field in ('input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens'):
            self.assertIsNone(empty_row[field], field)

    def test_identity_failure_returns_null_tokens_with_note(self):
        # First row valid, second row fails #9 + #10 == #3 (10 + 10 != 25).
        self.write_db([
            make_gen_row(inp=100, out=30, cache_write=0, cache_read=50, thinking=10, response=20, rid=b'r0'),
            make_gen_row(inp=100, out=25, cache_write=0, cache_read=50, thinking=10, response=10, rid=b'r1'),
        ])
        row = self.query()
        for field in ('input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens'):
            self.assertIsNone(row[field], field)
        self.assertIn('failed output identity (#9 + #10 != #3)', ' '.join(row['notes']))

    def test_missing_table_returns_null_tokens_with_note(self):
        self.write_db([], create_table=False)
        row = self.query()
        for field in ('model', 'input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens'):
            self.assertIsNone(row[field], field)
        self.assertIn('gen_metadata table missing; tokens unavailable', ' '.join(row['notes']))

    def test_missing_model_recovery_and_multiple_models(self):
        # Missing on every row -> model is None with note, tokens still populated.
        self.write_db([
            make_gen_row(inp=60, out=15, cache_write=0, cache_read=40, thinking=5, response=10, rid=b'r0', model=None),
        ])
        row = self.query()
        self.assertIsNone(row['model'])
        self.assertEqual(row['input_tokens'], 100)
        self.assertEqual(row['output_tokens'], 15)
        self.assertIn('model unavailable from record', ' '.join(row['notes']))

        # Missing on last row -> recovered from earlier row.
        self.db_path.unlink()
        self.write_db([
            make_gen_row(inp=60, out=15, cache_write=0, cache_read=40, thinking=5, response=10, rid=b'r0', model='recovered-model'),
            make_gen_row(inp=40, out=10, cache_write=0, cache_read=10, thinking=4, response=6, rid=b'r1', model=None),
        ])
        recovered = self.query()
        self.assertEqual(recovered['model'], 'recovered-model')
        self.assertEqual(recovered['input_tokens'], 150)

        # Multiple models -> last model kept, usd unavailable.
        self.db_path.unlink()
        self.write_db([
            make_gen_row(inp=60, out=15, cache_write=0, cache_read=40, thinking=5, response=10, rid=b'r0', model='first-model'),
            make_gen_row(inp=40, out=10, cache_write=0, cache_read=10, thinking=4, response=6, rid=b'r1', model='second-model'),
        ])
        price_file = self.home / 'prices.md'
        price_file.write_text(
            '| model id | input | cached input | output |\n'
            '| --- | --- | --- | --- |\n'
            '| second-model | $1.00 | $0.10 | $5.00 |\n',
            encoding='utf-8',
        )
        mixed = self.query(prices=price_file)
        self.assertEqual(mixed['model'], 'second-model')
        self.assertIsNone(mixed['usd'])
        self.assertIn('multiple models', ' '.join(mixed['notes']))

    def test_wal_rows_read_from_temp_copy_without_writing_live_dir(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER NOT NULL DEFAULT 0)')
        b0 = make_gen_row(inp=70, out=20, cache_write=0, cache_read=30, thinking=8, response=12, rid=b'wal-0')
        conn.execute('INSERT INTO gen_metadata (idx, data, size) VALUES (0, ?, ?)', (b0, len(b0)))
        conn.commit()
        # Keep connection open so WAL is active, copy .db and .db-wal without .db-shm into a fresh conversations dir.
        wal_src = Path(f'{self.db_path}-wal')
        self.assertTrue(wal_src.exists())
        live_home = Path(self.tmp.name) / 'live_home'
        live_conv = live_home / 'conversations'
        live_conv.mkdir(parents=True)
        (live_conv / f'{SID}.db').write_bytes(self.db_path.read_bytes())
        (live_conv / f'{SID}.db-wal').write_bytes(wal_src.read_bytes())
        conn.close()

        before_files = sorted(p.name for p in live_conv.iterdir())
        with patch.dict(os.environ, {'AGY_HOME': str(live_home)}):
            row = session_usage.query('agy', SID)
        after_files = sorted(p.name for p in live_conv.iterdir())
        self.assertEqual(before_files, [f'{SID}.db', f'{SID}.db-wal'])
        self.assertEqual(after_files, before_files)
        self.assertEqual(row['input_tokens'], 100)
        self.assertEqual(row['output_tokens'], 20)

    def test_missing_or_invalid_record_fails_with_path(self):
        with self.assertRaisesRegex(ValueError, 'Expected one Antigravity record at'):
            self.query()
        with self.assertRaises(ValueError):
            self.query(sid='../invalid')
        self.db_path.write_bytes(b'not a sqlite file')
        with self.assertRaisesRegex(ValueError, f'{SID}\\.db'):
            self.query()


if __name__ == '__main__':
    unittest.main()
