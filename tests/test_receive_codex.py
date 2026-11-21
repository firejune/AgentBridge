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


codex_queue_note = 'Codex Desktop queue accepted the Agent Bridge notification'


class ReceiveCodexTests(unittest.TestCase):
    def setUp(self):
        self.thread = str(uuid.uuid4())
        self.message_id = str(uuid.uuid4())
        self.body = 'Peer text requiring separate authorization.'
        self.message = {
            'id': self.message_id, 'sender': 'peer', 'recipient': 'target', 'project': 'demo',
            'body': self.body, 'reply_to': None, 'sender_channel': None, 'channel': None,
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

    def test_exact_recipient_reads_peer_envelope_and_marks_only_an_unread_message(self):
        def broker_request(path, fields, **kwargs):
            if fields['op'] == 'show':
                return self.message
            if self.message['state'] in ('dispatching', 'uncertain'):
                raise ValueError('Resolve delivery outcome first')
            return self.message | {'state': 'received'}

        with patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                patch.object(bridge.broker, 'request', side_effect=broker_request) as request:
            for state, ops, marked in (('dispatching', ['show', 'ack'], False),
                                       ('submitted', ['show', 'ack'], True),
                                       ('uncertain', ['show', 'ack'], False),
                                       ('received', ['show'], False),
                                       ('in_progress', ['show'], False)):
                with self.subTest(state=state):
                    request.reset_mock()
                    self.message['state'] = state
                    self.assertEqual(self.receive(), (0, {
                        'id': self.message_id, 'sender': 'peer',
                        'text': delivery.peer_text(self.message, 'test.sock', escape_peer=False),
                        'provenance': 'agent_bridge_peer', 'authorization': 'none',
                        'marked_received': marked,
                    }))
                    self.assertEqual([call.args[1] for call in request.call_args_list],
                                     [{'op': op, 'id': self.message_id} for op in ops])

    def test_a_failed_marker_never_fails_the_read(self):
        self.message['state'] = 'submitted'
        for error in (ValueError('Message is already complete'), OSError(errno.EIO, '/private/broker.sock'),
                      bridge.broker.BrokerResponseError('x'), RuntimeError('unexpected')):
            def broker_request(path, fields, **kwargs):
                if fields['op'] == 'show':
                    return self.message
                raise error

            with self.subTest(error=type(error)), patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                    patch.object(bridge.broker, 'request', side_effect=broker_request):
                status, received = self.receive()
                self.assertEqual((status, received['marked_received']), (0, False))
                self.assertEqual(received['text'], delivery.peer_text(self.message, 'test.sock', escape_peer=False))

    def test_queue_read_moves_submitted_to_received_and_the_reply_still_answers(self):
        with RunningBroker() as server:
            server.call('register', project='demo', name='peer', cwd=server.temp.name, transport='realtime')
            server.call('register', project='demo', name='target', cwd=server.temp.name,
                        transport='codex', session=self.thread)
            posted = server.call('send', project='demo', sender='peer', recipient='target',
                                 body=self.body, key='read-marker')
            mid = posted['id']
            server.call('claim', id=mid)
            with patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}):
                status, early = self.receive(mid, server.path)  # Still dispatching: refused, read intact.
                self.assertEqual((status, early['marked_received']), (0, False))
                self.assertEqual(server.call('show', id=mid)['state'], 'dispatching')
                server.call('finish', id=mid, state='submitted', note=codex_queue_note)
                submitted = server.call('show', id=mid)
                self.assertEqual(submitted['state'], 'submitted')
                status, read = self.receive(mid, server.path)
                self.assertEqual((status, read['marked_received']), (0, True))
                self.assertEqual(read['text'], early['text'])
                shown = server.call('show', id=mid)
                self.assertEqual(shown['state'], 'received')
                events = len(shown['events'])
                status, again = self.receive(mid, server.path)  # Already received: no second ack event.
                self.assertEqual((status, again['marked_received']), (0, False))
                self.assertEqual(len(server.call('show', id=mid)['events']), events)
            answer = server.call('reply', id=mid, body='Answer', key='read-marker-answer')
            self.assertEqual(answer['reply_to'], mid)
            self.assertEqual(server.call('show', id=mid)['state'], 'answered')

    def test_queue_reader_gets_the_envelope_and_reply_contract_direct_delivery_carries(self):
        with RunningBroker() as server:
            server.call('register', project='demo', name='peer', cwd=server.temp.name, transport='realtime')
            server.call('register', project='demo', name='target', cwd=server.temp.name,
                        transport='codex', session=self.thread)
            asked = server.call('send', project='demo', sender='peer', recipient='target',
                                body=self.body, key='asks')
            told = server.call('send-with-retention', project='demo', sender='peer', recipient='target',
                               body=self.body, key='tells', one_way=True)
            endings = (delivery.reply_command(asked, server.path), 'One-way message; no reply requested.')
            self.assertIn('--socket', endings[0])
            with patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}):
                for message, ending in zip((asked, told), endings):
                    server.call('claim', id=message['id'])
                    status, received = self.receive(message['id'], server.path)
                    shown = server.call('show', id=message['id'])
                    self.assertEqual(status, 0)
                    self.assertEqual(set(received), {'id', 'sender', 'text', 'provenance', 'authorization',
                                                     'marked_received'})
                    self.assertFalse(received['marked_received'])  # Read while still dispatching.
                    self.assertEqual(received['text'], delivery.peer_text(shown, server.path, escape_peer=False))
                    self.assertIn('Project: demo', received['text'])
                    self.assertIn(self.body, received['text'])
                    self.assertTrue(received['text'].endswith(ending), received['text'])
                    self.assertEqual(received['authorization'], 'none')
                    server.call('finish', id=message['id'], state='submitted')

    def test_message_without_envelope_fields_is_an_unusable_response(self):
        del self.message['project']
        with patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                patch.object(bridge.broker, 'request', return_value=self.message):
            self.assertEqual(self.receive(), (
                1, {'error': 'Agent Bridge broker did not return a usable message response.'}))

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

    def test_addressed_but_unreadable_messages_name_their_cause(self):
        # Was one fixed text for all of these (issue #164: an error names its cause).
        causes = [
            ({'state': 'pending'}, 'The addressed correspondence has not been dispatched yet.'),
            ({'state': 'failed'}, 'The addressed correspondence is already failed; nothing is left to receive.'),
            ({'state': 'done'}, 'The addressed correspondence is already done; nothing is left to receive.'),
            ({'state': 'answered'}, 'The addressed correspondence is already answered; nothing is left to receive.'),
            ({'state': 'failed', 'reason': 'archived'},
             'The addressed correspondence failed because this thread is archived.'),
            ({'reason': 'archived'}, 'The addressed correspondence failed because this thread is archived.'),
            ({'body_expired': True, 'body': None}, 'The addressed correspondence body has expired; only its record remains.'),
            ({'close_request': {}}, 'The addressed message is a close handoff; claim it with close-operator.'),
            ({'body': None}, 'Agent Bridge broker did not return a usable message response.'),
            ({'state': 'private text'}, 'Agent Bridge broker did not return a usable message response.'),
        ]
        for fields, expected in causes:
            with self.subTest(fields=fields), patch.dict('os.environ', {'CODEX_THREAD_ID': self.thread}), \
                    patch.object(bridge.broker, 'request', return_value=self.message | fields):
                self.assertEqual(self.receive(), (1, {'error': expected}))

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
                self.assertEqual(notification, codex_queue.notice(mid))
                self.assertGreater(timeout, 0)
                before = checkpoint.read_bytes()
                durable, = (item for item in json.loads(before)['messages'] if item['id'] == mid)
                self.assertEqual(durable['state'], 'dispatching')
                self.assertEqual(server.call('show', id=mid)['state'], 'dispatching')
                status, received = self.receive(mid, server.path)
                self.assertEqual((status, received['id'], received['sender']), (0, mid, 'peer'))
                self.assertIn(self.body, received['text'])
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
