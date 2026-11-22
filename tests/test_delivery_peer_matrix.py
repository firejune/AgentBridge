"""Issue #200: the claude-peer send/receive matrix through the CLI, asserted on what the broker records."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
import delivery
import sessions
from support import RunningBroker
from test_peer import FakeClaude, TOKEN
from test_wake import DESKTOP, HOST, WakeFixture

LIMIT = 32768


class RecordingClaude(FakeClaude):
    """FakeClaude that keeps the raw bytes, optionally answers on the push connection, and can hold the close."""
    def __init__(self, *args, answer=None, hold=None, **kwargs):
        self.raw, self.answer, self.hold = [], answer, hold
        self.arrived = threading.Event()
        super().__init__(*args, **kwargs)

    def accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                data = b''
                while chunk := conn.recv(65536):
                    data += chunk
                self.raw.append(data)
                self.received.append([json.loads(line) for line in data.decode('utf-8').splitlines()])
                self.arrived.set()
                if self.hold is not None:
                    self.hold.wait(5)
                if self.answer is not None:
                    try:
                        conn.sendall(self.answer)
                    except OSError:
                        pass


class CliFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='abm-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.sid = str(uuid.uuid4())
        self.fakes = []
        self.env = {'CLAUDE_CONFIG_DIR':self.temp.name, 'CLAUDE_DESKTOP_SESSIONS_DIR':str(self.root/'desktop')}

    def tearDown(self):
        for fake in self.fakes:
            fake.close()
        self.temp.cleanup()

    def fake(self, cls=RecordingClaude, **kwargs):
        fake = cls(self.temp.name, self.sid, **kwargs)
        self.fakes.append(fake)
        return fake

    def register(self, server):
        server.call('register', project='demo', name='alpha', cwd=self.temp.name)
        server.call('register', project='demo', name='beta', cwd=self.temp.name,
                    transport='claude-peer', session=self.sid)

    def main(self, server, *args, body='Question'):
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch('sys.stdout', out), \
                mock.patch('sys.stdin', io.StringIO(body)), mock.patch.object(delivery, 'TITLE_WAIT', 0.0):
            code = bridge.main(['--socket', server.path, *args])
        return code, json.loads(out.getvalue().splitlines()[-1])

    def send(self, server, key='k1', body='Question'):
        return self.main(server, 'send', '--project', 'demo', '--from', 'alpha', '--to', 'beta',
                         '--key', key, '--file', '-', body=body)

    def states(self, server, mid):
        stored = server.call('show', id=mid)
        return stored, [(e['state'], e.get('event')) for e in stored['events']]

    def key_file(self, fake):
        import hashlib
        digest = hashlib.sha256(os.path.abspath(fake.sock).encode()).hexdigest()
        return fake.registry/f'{fake.pid}.{digest}.key'


class ReceiverAnswerAndKeyFileTests(CliFixture):
    """Gap 1: a receiver that answers is uncertain (bytes were written); a bad key file is pending (nothing written)."""

    def test_receiver_answering_on_the_push_connection_is_uncertain_and_blocks_the_recipient(self):
        fake = self.fake(answer=b'{"type":"ack"}\n')
        with RunningBroker() as server:
            self.register(server)
            code, out = self.send(server)
            self.assertEqual(code, 1)
            self.assertTrue(out['push_failed'])
            self.assertIn('answered on the push connection', out['error'])
            stored, events = self.states(server, out['message']['id'])
            self.assertEqual(stored['state'], 'uncertain')
            self.assertEqual(events, [('dispatching', None), ('uncertain', None)])
            self.assertIn('answered on the push connection', stored['events'][-1]['note'])
            self.assertEqual(len(fake.received), 1)  # Written once; never resent on our own.
            later = server.call('send', project='demo', sender='alpha', recipient='beta', body='Later', key='k2')
            self.assertEqual(server.call('show', id=later['id'])['blocked_by']['id'], out['message']['id'])
            self.assertNotIn(TOKEN, json.dumps(out))

    def test_missing_key_file_is_pending_and_connects_nothing(self):
        fake = self.fake()
        self.key_file(fake).unlink()
        with RunningBroker() as server:
            self.register(server)
            code, out = self.send(server)
            self.assertEqual(code, 1)
            self.assertIn('no peer key file', out['error'])
            stored, events = self.states(server, out['message']['id'])
            self.assertEqual(stored['state'], 'pending')
            self.assertEqual(events, [('dispatching', None), ('pending', 'push_failed')])
            self.assertIn('push_failed: Claude session has no peer key file', stored['events'][-1]['note'])
            self.assertEqual(fake.raw, [])
            later = server.call('send', project='demo', sender='alpha', recipient='beta', body='Later', key='k2')
            self.assertNotIn('blocked_by', server.call('show', id=later['id']))

    def test_malformed_key_files_are_pending_and_never_echo_the_content(self):
        bad = {'not json': '{peerToken', 'json list': json.dumps([TOKEN]), 'short': json.dumps({'peerToken':'ab'*15}),
               'uppercase': json.dumps({'peerToken':TOKEN.upper()}), 'number': json.dumps({'peerToken':12345}),
               'missing': json.dumps({'token':TOKEN}), 'empty': ''}
        fake = self.fake()
        with RunningBroker() as server:
            self.register(server)
            for n, (label, content) in enumerate(bad.items()):
                with self.subTest(label):
                    self.key_file(fake).write_text(content)
                    code, out = self.send(server, key=f'bad-{n}')
                    self.assertEqual(code, 1)
                    self.assertIn('peer key file is malformed', out['error'])
                    stored, events = self.states(server, out['message']['id'])
                    self.assertEqual(stored['state'], 'pending')
                    self.assertEqual(events[-1], ('pending', 'push_failed'))
                    self.assertNotIn(TOKEN, json.dumps(out))
                    self.assertNotIn(TOKEN.upper(), json.dumps(out))
            self.assertEqual(fake.raw, [])


class SameKeyReplayTests(CliFixture):
    """Gap 2: `send` again with the same key and body."""

    def test_replay_of_a_pending_message_pushes_it_once(self):
        absent = self.fake(cls=FakeClaude, listen=False)  # Registry and key file, no inbox yet.
        with RunningBroker() as server:
            self.register(server)
            code, out = self.send(server)
            self.assertEqual((code, out['message']['state']), (1, 'pending'))
            mid = out['message']['id']
            absent.close()
            fake = self.fake()  # The inbox comes up on the same socket path.
            code, again = self.send(server)
            self.assertEqual(code, 0)
            self.assertEqual((again['id'], again['state']), (mid, 'submitted'))
            stored, events = self.states(server, mid)
            self.assertEqual(events, [('dispatching', None), ('pending', 'push_failed'),
                                      ('dispatching', None), ('submitted', 'pushed')])
            self.assertEqual(len(fake.received), 1)
            self.assertIn(mid, fake.received[0][1]['message']['content'])
            # A third replay of the now-submitted message pushes nothing.
            self.assertEqual(self.send(server)[0], 0)
            self.assertEqual(len(fake.received), 1)

    def test_replay_of_a_dispatching_message_exits_zero_and_of_an_uncertain_one_exits_one_without_pushing(self):
        # Ruling on #200: an uncertain replay answers like the first call (exit 1, uncertain, id).
        absent = self.fake(cls=FakeClaude, listen=False)
        with RunningBroker() as server:
            self.register(server)
            code, out = self.send(server)
            mid = out['message']['id']
            absent.close()
            fake = self.fake()
            server.call('claim', id=mid)
            for state in ('dispatching', 'uncertain'):
                with self.subTest(state=state):
                    if state == 'uncertain':
                        server.call('finish', id=mid, state='uncertain', note='bytes may have reached the receiver')
                    before = server.call('show', id=mid)
                    code, again = self.send(server)
                    if state == 'dispatching':
                        self.assertEqual(code, 0)
                        self.assertEqual((again['id'], again['state']), (mid, state))
                        self.assertNotIn('uncertain', again)
                    else:
                        self.assertEqual(code, 1)
                        self.assertEqual((again['id'], again['uncertain'], again['stage']), (mid, True, 'replay'))
                        self.assertIn(mid, again['error'])
                    after = server.call('show', id=mid)
                    self.assertEqual((after['state'], after['events']), (state, before['events']))
                    self.assertEqual(fake.raw, [])

    def test_reply_replay_of_an_uncertain_reply_exits_one_without_pushing(self):
        fake = self.fake()
        with RunningBroker() as server:
            self.register(server)
            parent = server.call('send', project='demo', sender='beta', recipient='alpha', body='Question', key='q')
            reply = lambda: self.main(server, 'reply', parent['id'], '--key', 'r1', '--file', '-', body='Answer')
            first = server.call('reply', id=parent['id'], key='r1', body='Answer')
            self.assertEqual(first['route']['transport'], 'claude-peer')
            server.call('claim', id=first['id'])
            server.call('finish', id=first['id'], state='uncertain', note='bytes may have reached the receiver')
            before = server.call('show', id=first['id'])['events']
            code, again = reply()
            self.assertEqual((code, again['id'], again['uncertain']), (1, first['id'], True))
            self.assertEqual(server.call('show', id=first['id'])['events'], before)
            server.call('resolve', id=first['id'], state='submitted', note='operator saw it arrive')
            code, again = reply()
            self.assertEqual((code, again['state']), (0, 'submitted'))
            self.assertEqual(fake.raw, [])

    def uncertain_first(self, server, run):
        """Run `run` once against an absent inbox (pending), mark the message uncertain, bring the inbox up."""
        absent = self.fake(cls=FakeClaude, listen=False)
        self.register(server)
        code, out = run()
        self.assertEqual(code, 1)
        mid = (out.get('message') or out)['id']
        absent.close()
        fake = self.fake()
        server.call('claim', id=mid)
        server.call('finish', id=mid, state='uncertain', note='bytes may have reached the receiver')
        return mid, fake, server.call('show', id=mid)

    def test_ask_replay_of_an_uncertain_message_exits_one_without_pushing_or_waiting(self):
        with RunningBroker() as server:
            ask = lambda: self.main(server, 'ask', '--project', 'demo', '--from', 'alpha', '--to', 'beta',
                                    '--key', 'a1', '--file', '-', '--timeout', '30')
            mid, fake, before = self.uncertain_first(server, ask)
            started = time.monotonic()
            code, again = ask()
            self.assertLess(time.monotonic() - started, 5)  # Did not go on to wait for a reply.
            self.assertEqual((code, again['id'], again['uncertain'], again['stage']), (1, mid, True, 'replay'))
            after = server.call('show', id=mid)
            self.assertEqual((after['state'], after['events']), ('uncertain', before['events']))
            self.assertEqual(fake.raw, [])

    def test_handover_replay_of_an_uncertain_message_exits_one_without_pushing(self):
        rows = [{'claim':'unit', 'exit':0, 'figures':'1/1', 'receipt':'r1', 'receipt_kind':'run',
                 'head':'a'*40, 'recorded_head':'a'*40}]
        with RunningBroker() as server, mock.patch.object(bridge.receipts, 'check', return_value=rows):
            handover = lambda: self.main(server, 'handover', '--rows', 'unused', '--head', 'a'*40,
                                         '--project', 'demo', '--from', 'alpha', '--to', 'beta', '--card', '#200')
            mid, fake, before = self.uncertain_first(server, handover)
            code, again = handover()
            self.assertEqual((code, again['id'], again['uncertain'], again['stage']), (1, mid, True, 'replay'))
            after = server.call('show', id=mid)
            self.assertEqual((after['state'], after['events']), ('uncertain', before['events']))
            self.assertEqual(fake.raw, [])

    def test_replay_with_a_different_body_is_refused_and_records_nothing(self):
        fake = self.fake()
        with RunningBroker() as server:
            self.register(server)
            code, out = self.send(server)
            self.assertEqual(code, 0)
            before = server.call('show', id=out['id'])['events']
            code, again = self.send(server, body='Something else')
            self.assertEqual(code, 1)
            self.assertIn('different content', again['error'])
            self.assertEqual(server.call('show', id=out['id'])['events'], before)
            self.assertEqual(len(server.call('inbox', project='demo', name='beta')), 1)
            self.assertEqual(len(fake.received), 1)


class MultibyteBodyTests(CliFixture):
    """Gap 3: the body limit counts UTF-8 bytes, and a unicode push reaches the receiver intact."""

    def test_multibyte_body_at_the_limit_is_pushed_intact(self):
        body = '가'*((LIMIT-2)//3) + 'ab'
        self.assertEqual((len(body.encode()), len(body) < LIMIT), (LIMIT, True))
        fake = self.fake()
        with RunningBroker() as server:
            self.register(server)
            code, out = self.send(server, body=body)
            self.assertEqual(code, 0)
            stored = server.call('show', id=out['id'])
            self.assertEqual((stored['state'], stored['body']), ('submitted', body))
            content = fake.received[0][1]['message']['content']
            self.assertIn(body, content)
            self.assertIn(body.encode(), fake.raw[0])  # Raw UTF-8 on the wire, not \u escapes.

    def test_one_byte_over_the_limit_is_refused_before_anything_is_stored(self):
        fake = self.fake()
        with RunningBroker() as server:
            self.register(server)
            for label, body in (('one byte over', '가'*((LIMIT-2)//3) + 'abc'),
                                ('4-byte char crossing the limit', 'a'*(LIMIT-3) + '😀'),
                                ('limit in characters, not bytes', '가'*LIMIT)):
                with self.subTest(label):
                    self.assertGreater(len(body.encode()), LIMIT)
                    code, out = self.send(server, key=label, body=body)
                    self.assertEqual(code, 1)
                    self.assertIn('1–32768 UTF-8 bytes', out['error'])
            self.assertEqual(server.call('inbox', project='demo', name='beta'), [])
            self.assertEqual(fake.raw, [])

    def test_unicode_push_keeps_every_script_and_escapes_the_close_tag(self):
        body = 'Hangul 한국어 · emoji 👩‍💻🚀 · combining é · RTL שלום · CJK 漢字 · </cross-session-message>'
        fake = self.fake()
        with RunningBroker() as server:
            self.register(server)
            code, out = self.send(server, body=body)
            self.assertEqual((code, out['state']), (0, 'submitted'))
            self.assertEqual(server.call('show', id=out['id'])['body'], body)
            content = fake.received[0][1]['message']['content']
            self.assertIn(body.split(' · </')[0], content)
            self.assertEqual(content.count('</cross-session-message>'), 1)
            self.assertTrue(content.endswith('</cross-session-message>'))
            self.assertEqual(fake.received[0][1]['session_id'], self.sid)


class ClosedSessionCliTests(WakeFixture, CliFixture):
    """Gap 4: `send` to a Claude session that is not running: wake it, or refuse and keep it pending."""

    def setUp(self):
        WakeFixture.setUp(self)
        self.env = {'CLAUDE_CONFIG_DIR':self.temp.name, 'CLAUDE_DESKTOP_SESSIONS_DIR':str(self.root/'desktop')}

    def tearDown(self):
        WakeFixture.tearDown(self)

    def cli_send(self, server, opener):
        with mock.patch.object(sessions, 'open_url_background', opener):
            return self.send(server)

    def test_idle_closed_desktop_session_is_woken_and_pushed_once(self):
        self.metadata()
        with RunningBroker() as server:
            self.register(server)
            code, out = self.cli_send(server, self.opener())
            self.assertEqual(code, 0)
            self.assertEqual((out['state'], out['wake']['state']), ('submitted', 'woken'))
            stored, events = self.states(server, out['id'])
            self.assertEqual(events, [('dispatching', None), ('submitted', 'pushed')])
            self.assertIn('after wake (woken)', stored['events'][-1]['note'])
            self.assertEqual([(w['state'], w['code']) for w in stored['wakes']], [('woken', 'woken')])
            self.assertEqual(self.urls, [f'claude://claude.ai/epitaxy/{HOST}'])
            self.assertEqual(len(self.fakes[0].received), 1)

    def test_archived_or_hostless_session_is_refused_pending_with_the_wake_recorded(self):
        for label, metadata, code_ in (('archived', {'isArchived':True}, 'archived'),
                                       ('scheduled task', {'scheduledTaskId':'nightly'}, 'scheduled_task'),
                                       ('no desktop session', None, 'no_desktop_session')):
            with self.subTest(label):
                for path in (self.root/'desktop').rglob('*.json'):
                    path.unlink()
                if metadata is not None:
                    self.metadata(**metadata)
                with RunningBroker() as server:
                    self.register(server)
                    code, out = self.cli_send(server, self.opener())
                    self.assertEqual(code, 1)
                    self.assertTrue(out['push_failed'])
                    self.assertIn('wake unsupported', out['error'])
                    self.assertEqual(out['message']['wake']['code'], code_)
                    stored, events = self.states(server, out['message']['id'])
                    self.assertEqual(stored['state'], 'pending')
                    self.assertEqual(events, [('dispatching', None), ('pending', 'push_failed')])
                    self.assertIn('wake unsupported', stored['events'][-1]['note'])
                    self.assertEqual([(w['state'], w['code']) for w in stored['wakes']], [('unsupported', code_)])
                    later = server.call('send', project='demo', sender='alpha', recipient='beta', body='L', key='k2')
                    self.assertNotIn('blocked_by', server.call('show', id=later['id']))
                self.assertEqual(self.urls, [])
                self.assertEqual(self.fakes, [])


class SerialPeerDeliveryTests(CliFixture):
    """Gap 5: two pushes to one live peer; the second waits behind the first's claim."""

    def push_first(self, server, message, results):
        def run():
            try:
                results.append(bridge.push(server.path, message))
            except bridge.PushFailed as error:
                results.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        return thread

    def two(self, server):
        self.register(server)
        return [server.call('send', project='demo', sender='alpha', recipient='beta', body=f'Q{n}', key=f'k{n}')
                for n in (1, 2)]

    def test_second_push_to_a_live_peer_goes_after_the_first_finishes(self):
        hold = threading.Event()
        fake = self.fake(hold=hold)
        with RunningBroker() as server, mock.patch.dict(os.environ, self.env), \
                mock.patch.object(delivery, 'TITLE_WAIT', 0.0):
            first, second = self.two(server)
            results = []
            thread = self.push_first(server, first, results)
            self.assertTrue(fake.arrived.wait(5))
            self.assertEqual(server.call('show', id=first['id'])['state'], 'dispatching')
            self.assertEqual(server.call('show', id=second['id'])['blocked_by']['id'], first['id'])
            threading.Timer(0.4, hold.set).start()
            later = bridge.push(server.path, second)
            thread.join(10)
            self.assertEqual(results[0]['state'], 'submitted')
            self.assertEqual((later['state'], later['behind']), ('submitted', {'id':first['id'], 'state':'submitted'}))
            frames = [user['message']['content'] for _, user in fake.received]
            self.assertEqual([sum(m['id'] in f for f in frames) for m in (first, second)], [1, 1])
            self.assertIn(first['id'], frames[0])  # Order kept.
            _, events = self.states(server, second['id'])
            self.assertEqual(events, [('dispatching', None), ('submitted', 'pushed')])

    def test_second_push_is_held_when_the_first_ends_uncertain_by_design(self):
        # Intended (delivery.claim_behind docstring): "Any other end leaves `m` pending ... nothing is sent."
        hold = threading.Event()
        fake = self.fake(hold=hold, answer=b'unexpected\n')
        with RunningBroker() as server, mock.patch.dict(os.environ, self.env):
            first, second = self.two(server)
            results = []
            thread = self.push_first(server, first, results)
            self.assertTrue(fake.arrived.wait(5))
            threading.Timer(0.4, hold.set).start()
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path, second)
            thread.join(10)
            self.assertIsInstance(results[0], bridge.PushFailed)
            self.assertEqual(server.call('show', id=first['id'])['state'], 'uncertain')
            self.assertRegex(str(caught.exception), rf"^pending behind {first['id']} since \S+ \(uncertain\); not sent")
            stored, events = self.states(server, second['id'])
            self.assertEqual(stored['state'], 'pending')
            self.assertEqual(events, [('pending', None)])  # Never claimed; only the durable behind note.
            self.assertTrue(stored['note'].startswith(f"pending behind {first['id']} since "))  # Live blocker note.
            self.assertEqual(stored['blocked_by']['state'], 'uncertain')
            self.assertIn('(uncertain); not sent', stored['events'][-1]['note'])
            self.assertEqual(len(fake.received), 1)

    def test_second_push_is_held_when_the_first_fails_before_writing_by_design(self):
        # Intended (delivery.claim_behind docstring): "Any other end leaves `m` pending ... nothing is sent."
        fake = self.fake()
        entered, go = threading.Event(), threading.Event()
        real_token = delivery.peer_token

        def slow_token(target):
            if not entered.is_set():
                entered.set()
                go.wait(5)
                raise ValueError('Claude session has no peer key file; protocol may have changed')
            return real_token(target)
        with RunningBroker() as server, mock.patch.dict(os.environ, self.env), \
                mock.patch.object(delivery, 'peer_token', slow_token):
            first, second = self.two(server)
            results = []
            thread = self.push_first(server, first, results)
            self.assertTrue(entered.wait(5))
            threading.Timer(0.4, go.set).start()
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path, second)
            thread.join(10)
            self.assertEqual(server.call('show', id=first['id'])['state'], 'pending')
            self.assertIn('(pending); not sent', str(caught.exception))
            self.assertEqual(server.call('show', id=second['id'])['state'], 'pending')
            self.assertEqual(fake.raw, [])
            # Both stay deliverable: a later delivery sends each exactly once, in order.
            with mock.patch.object(delivery, 'TITLE_WAIT', 0.0):
                for m in (first, second):
                    self.assertEqual(bridge.push(server.path, server.call('show', id=m['id']))['state'], 'submitted')
            self.assertEqual(len(fake.received), 2)


if __name__ == '__main__':
    unittest.main()
