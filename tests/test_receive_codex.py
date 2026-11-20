from contextlib import redirect_stdout
import errno
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import codex_live
import codex_queue
import delivery
from support import RunningBroker


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

    def receive(self, message_id=None, socket='test.sock'):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = bridge.main(['--socket', socket, 'receive-codex', message_id or self.message_id])
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

    def test_unknown_message_and_wrong_destinations_are_indistinguishable(self):
        expected = (1, {'error': 'No correspondence matches this Codex thread and message ID.'})
        with patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                patch.object(bridge.broker, 'request',
                             side_effect=ValueError('Unknown message; broker may have restarted')):
            self.assertEqual(self.receive(), expected)
        invalid_fields = [
            {'id': str(uuid.uuid4())},
            {'route': {'transport': 'codex', 'session': str(uuid.uuid4())}},
            {'route': {'transport': 'claude-peer', 'session': self.thread}},
            {'route': {'transport': 'codex', 'session': str(uuid.uuid4())}, 'state': 'pending'},
        ]
        for fields in invalid_fields:
            with self.subTest(fields=fields), patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                    patch.object(bridge.broker, 'request', return_value=self.message | fields):
                self.assertEqual(self.receive(), expected)

    def test_addressed_but_unreadable_messages_return_a_fixed_diagnostic(self):
        invalid_fields = [
            *({'state': state} for state in ('pending', 'failed', 'done', 'answered')),
            {'reason': 'archived'}, {'body_expired': True}, {'close_request': {}}, {'body': None},
        ]
        for fields in invalid_fields:
            with self.subTest(fields=fields), patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                    patch.object(bridge.broker, 'request', return_value=self.message | fields):
                self.assertEqual(self.receive(), (
                    1, {'error': 'The addressed correspondence is not available to receive.'}))

    def test_socket_errors_preserve_only_the_error_category(self):
        cases = (
            (errno.EPERM, 'Agent Bridge broker socket access was denied. Check shell sandbox and socket permissions.'),
            (errno.EACCES, 'Agent Bridge broker socket access was denied. Check shell sandbox and socket permissions.'),
            (errno.ENOENT, 'Agent Bridge broker is unavailable for this socket.'),
            (errno.ECONNREFUSED, 'Agent Bridge broker is unavailable for this socket.'),
        )
        for error_number, expected in cases:
            with self.subTest(error=error_number), patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                    patch.object(bridge.broker.socket, 'socket') as socket:
                connect = socket.return_value.__enter__.return_value.connect
                connect.side_effect = OSError(error_number, '/private/broker.sock')
                self.assertEqual(self.receive(), (1, {'error': expected}))

    def test_broker_read_or_protocol_error_does_not_reveal_private_details(self):
        expected = (1, {'error': 'Agent Bridge broker did not return a usable message response.'})
        for error in (ValueError(self.body), OSError(errno.EIO, '/private/broker.sock')):
            with self.subTest(error_type=type(error)), patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                    patch.object(bridge.broker, 'request', side_effect=error):
                self.assertEqual(self.receive(), expected)

    def test_queue_enqueue_observes_a_durable_readable_claim(self):
        endpoint = codex_live.EndpointUnavailable
        with RunningBroker() as server:
            server.call('register', project='demo', name='peer', cwd=server.temp.name, transport='realtime')
            server.call('register', project='demo', name='target', cwd=server.temp.name,
                        transport='codex', session=self.thread)
            posted = server.call('send', project='demo', sender='peer', recipient='target',
                                 body=self.body, key='queue-claim')
            mid = posted['id']
            checkpoint = Path(server.state_dir) / 'correspondence.json'

            def enqueue(thread, notification, timeout):
                self.assertEqual(thread, self.thread)
                self.assertEqual(notification, codex_queue.NOTICE + mid)
                self.assertGreater(timeout, 0)
                before = checkpoint.read_bytes()
                durable, = (item for item in json.loads(before)['messages'] if item['id'] == mid)
                self.assertEqual(durable['state'], 'dispatching')
                self.assertEqual(server.call('show', id=mid)['state'], 'dispatching')
                self.assertEqual(self.receive(mid, server.path), (0, {
                    'id': mid, 'sender': 'peer', 'body': self.body,
                    'provenance': 'agent_bridge_peer', 'authorization': 'none',
                }))
                self.assertEqual(checkpoint.read_bytes(), before)
                return str(uuid.uuid4())

            with patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                    patch.object(bridge.broker, 'DEFAULT_SOCKET', Path(server.path)), \
                    patch.object(codex_live, 'exact_saved_workspace', return_value=(None, None)), \
                    patch.object(codex_live, 'Client') as client, \
                    patch.object(codex_queue, 'check_route'), \
                    patch.object(codex_queue, 'enqueue', side_effect=enqueue) as queued:
                client.return_value.mutation_sent = False
                client.return_value.__enter__.side_effect = endpoint(endpoint.DEFAULT, endpoint.MISSING)
                pushed = delivery.push_codex(server.path, posted)
            queued.assert_called_once()
            self.assertEqual(pushed['delivery_path'], codex_queue.DELIVERY_PATH)
            self.assertEqual(server.call('show', id=mid)['state'], 'submitted')


if __name__ == '__main__':
    unittest.main()
