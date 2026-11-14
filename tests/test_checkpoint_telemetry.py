"""Checkpoint telemetry never changes delivery or persistence semantics."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import broker
import message_store
import route_store
from support import RunningBroker


class CheckpointTelemetryTests(unittest.TestCase):
    def test_private_broker_ping_reports_updated_save_metrics(self):
        with RunningBroker() as running:
            running.register_pair()
            before = running.call('ping')
            self.assertGreaterEqual(before['checkpoint_last_save_seconds'], 0)
            self.assertGreaterEqual(before['checkpoint_max_save_seconds'],
                                    before['checkpoint_last_save_seconds'])
            self.assertEqual(before['checkpoint_bytes'],
                             (Path(running.state_dir) / 'correspondence.json').stat().st_size)
            sent = running.call('send', project='demo', sender='alpha', recipient='beta',
                                key='telemetry', body='Question')
            after = running.call('ping')
            self.assertGreater(after['checkpoint_bytes'], before['checkpoint_bytes'])
            self.assertGreaterEqual(after['checkpoint_max_save_seconds'],
                                    before['checkpoint_max_save_seconds'])
            self.assertEqual(running.call('show', id=sent['id'])['state'], 'pending')

    def test_slow_save_logs_once_and_max_survives_a_faster_save(self):
        with tempfile.TemporaryDirectory(prefix='ab-telemetry-', dir='/tmp') as root:
            store = route_store.RouteStore(Path(root) / 'state')
            self.addCleanup(store.close)
            state = broker.State(store=store)
            output = io.StringIO()
            write = message_store.atomic_write
            delayed = False

            def slow_write(*args, **kwargs):
                nonlocal delayed
                if not delayed:
                    delayed = True
                    time.sleep(1.05)
                return write(*args, **kwargs)

            with redirect_stderr(output), patch.object(message_store, 'atomic_write', slow_write):
                state.correspondence.save(state)
            slow = state.action({'op': 'ping'})
            self.assertGreaterEqual(slow['checkpoint_last_save_seconds'], 1)
            log, = output.getvalue().splitlines()
            record = json.loads(log)
            self.assertEqual(record['event'], 'checkpoint_save_slow')
            self.assertEqual(record['duration_seconds'], slow['checkpoint_last_save_seconds'])
            self.assertEqual(record['bytes'], slow['checkpoint_bytes'])
            self.assertEqual(record['retained_count'], 0)
            self.assertEqual(set(record), {'event', 'duration_seconds', 'bytes', 'retained_count'})
            with redirect_stderr(io.StringIO()):
                state.correspondence.save(state)
            fast = state.action({'op': 'ping'})
            self.assertLess(fast['checkpoint_last_save_seconds'], slow['checkpoint_last_save_seconds'])
            self.assertEqual(fast['checkpoint_max_save_seconds'], slow['checkpoint_max_save_seconds'])

    def test_failed_save_keeps_size_and_records_duration_without_acknowledging(self):
        with tempfile.TemporaryDirectory(prefix='ab-telemetry-', dir='/tmp') as root:
            store = route_store.RouteStore(Path(root) / 'state')
            self.addCleanup(store.close)
            state = broker.State(store=store)
            for name in ('alpha', 'beta'):
                state.action(dict(op='register', project='demo', name=name,
                                  cwd=root, transport='realtime'))
            before = state.action({'op': 'ping'})
            with patch.object(message_store, 'atomic_write', side_effect=OSError('fixture failure')):
                with self.assertRaisesRegex(OSError, 'fixture failure'):
                    state.action(dict(op='send', project='demo', sender='alpha',
                                      recipient='beta', key='failed', body='Question'))
            after = state.action({'op': 'ping'})
            self.assertEqual(after['messages'], 0)
            self.assertEqual(after['checkpoint_bytes'], before['checkpoint_bytes'])
            self.assertGreaterEqual(after['checkpoint_last_save_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
