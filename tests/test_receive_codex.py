from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge


class ReceiveCodexTests(unittest.TestCase):
    def setUp(self):
        self.thread = str(uuid.uuid4())
        self.message_id = str(uuid.uuid4())
        self.body = 'Peer text requiring separate authorization.'
        self.message = {
            'id': self.message_id, 'sender': 'peer', 'body': self.body,
            'state': 'dispatching',
            'route': {'transport': 'codex', 'session': self.thread, 'cwd': '/private/recipient'},
            'return_route': {'transport': 'claude-peer', 'session': str(uuid.uuid4())},
            'note': 'private diagnostic',
        }

    def receive(self, message_id=None):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = bridge.main(['--socket', 'test.sock', 'receive-codex', message_id or self.message_id])
        return result, json.loads(stdout.getvalue())

    def test_exact_recipient_reads_peer_envelope_without_mutating_broker(self):
        with patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                patch.object(bridge.broker, 'request', return_value=self.message) as request:
            for state in ('dispatching', 'submitted', 'uncertain', 'received', 'in_progress'):
                with self.subTest(state=state):
                    request.reset_mock()
                    self.message['state'] = state
                    self.assertEqual(self.receive(), (0, {
                        'id': self.message_id, 'sender': 'peer', 'body': self.body,
                        'provenance': 'agent_bridge_peer', 'authorization': 'none',
                    }))
                    request.assert_called_once_with('test.sock', {'op': 'show', 'id': self.message_id})

    def test_missing_caller_or_invalid_opaque_id_does_not_access_broker(self):
        with patch.object(bridge.broker, 'request') as request:
            for caller, message_id in (('', self.message_id), ('invalid', self.message_id),
                                       (self.thread, 'prefix'), (self.thread, self.message_id.upper()),
                                       (self.thread, self.message_id.replace('-', ''))):
                with self.subTest(caller=caller, message_id=message_id), \
                        patch.dict('os.environ', {'CODEX_THREAD_ID': caller}):
                    status, payload = self.receive(message_id)
                    self.assertEqual(status, 1)
                    self.assertEqual(set(payload), {'error'})
            request.assert_not_called()

    def test_unrelated_non_dispatched_archived_and_expired_are_indistinguishable(self):
        invalid_fields = [
            {'id': str(uuid.uuid4())},
            {'route': {'transport': 'codex', 'session': str(uuid.uuid4())}},
            {'route': {'transport': 'claude-peer', 'session': self.thread}},
            *({'state': state} for state in ('pending', 'failed', 'done', 'answered')),
            {'reason': 'archived'},
            {'body_expired': True},
            {'close_request': {}},
            {'body': None},
        ]
        with patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                patch.object(bridge.broker, 'request', side_effect=ValueError(self.body)):
            expected = self.receive()
        self.assertEqual(expected[0], 1)
        self.assertEqual(set(expected[1]), {'error'})
        self.assertNotIn(self.body, expected[1]['error'])
        for fields in invalid_fields:
            with self.subTest(fields=fields), patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                    patch.object(bridge.broker, 'request', return_value=self.message | fields):
                self.assertEqual(self.receive(), expected)


if __name__ == '__main__':
    unittest.main()
