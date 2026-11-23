"""MCP facade delivery through real pushes (issue #202)."""
from contextlib import closing, contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import broker
import codex_queue
import mcp_phase1 as phase
import mcp_server
import sessions
from support import RunningBroker
from test_codex_live import FakeDesktop
from test_peer import FakeClaude, TOKEN, dead_pid

try:
    import jsonschema
except ImportError:
    jsonschema = None


class RealMcpDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.running = RunningBroker().__enter__()
        self.addCleanup(self.running.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory(prefix='ab202-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = str(Path(self.temp.name).resolve())
        self.claude_sid = str(uuid.uuid4())
        self.codex_sid = str(uuid.uuid4())
        self.project = 'test-project'
        self.config = {
            'principal': 'test-owner',
            'tunnel_id': 'test-real-delivery',
            'writes_enabled': True,
            'scopes': list(set(phase.SCOPES.values())),
            'state_dir': self.root + '/ledger',
            'inbox_enabled': True,
            'workspaces': {
                'claude-ws': {
                    'cwd': self.root,
                    'root': self.root,
                    'project': self.project,
                    'ide': next(iter(sessions.CLAUDE_IDES)),
                    'agent_type': 'claude',
                },
                'codex-ws': {
                    'cwd': self.root,
                    'root': self.root,
                    'project': self.project,
                    'agent_type': 'codex',
                },
            },
            'pinned_sessions': {
                'claude-pin': {
                    'uuid': self.claude_sid,
                    'cwd': self.root,
                    'project': self.project,
                    'route': 'claude-worker',
                    'authorized_at': phase.stamp(),
                    'agent_type': 'claude',
                },
                'codex-pin': {
                    'uuid': self.codex_sid,
                    'cwd': self.root,
                    'project': self.project,
                    'route': 'codex-worker',
                    'authorized_at': phase.stamp(),
                    'agent_type': 'codex',
                },
            },
        }
        self.running.call('register', project=self.project, name='claude-worker',
                          cwd=self.root, transport='claude-peer', session=self.claude_sid)
        self.running.call('register', project=self.project, name='codex-worker',
                          cwd=self.root, transport='codex', session=self.codex_sid)
        self.backend = phase.Backend(self.running.path)
        self.engine = phase.Engine(self.config, self.backend, start=False)
        self.addCleanup(lambda: self.engine.close())
        pins = phase.pinned_sessions(self.config)
        self.claude_pin = next(s for s in pins.values() if s['alias'] == 'claude-pin')
        self.codex_pin = next(s for s in pins.values() if s['alias'] == 'codex-pin')
        for s in (self.claude_pin, self.codex_pin):
            self.running.call('register', project=self.project, name=s['sender'],
                              cwd=self.root, transport='realtime')
        self.fakes = []

    def tearDown(self):
        for fake in self.fakes:
            fake.close()

    def fake_claude(self, sid=None, **kwargs):
        fake = FakeClaude(self.root, sid or self.claude_sid, **kwargs)
        self.fakes.append(fake)
        return fake

    def call(self, name, **args):
        out = mcp_server.handle({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': args},
        }, self.running.path, self.engine)
        payload = out['result']['structuredContent']
        self.assertEqual(json.loads(out['result']['content'][0]['text']), payload)
        if jsonschema:
            jsonschema.validate(payload, next(t['outputSchema'] for t in phase.TOOLS if t['name'] == name))
        return payload

    def data(self, name, **args):
        out = self.call(name, **args)
        self.assertTrue(out['ok'], out)
        return out['data']

    def test_queue_conflict_reaches_cli_and_mcp_without_enqueue_or_durable_process_data(self):
        endpoint = phase.codex_live.EndpointUnavailable(
            phase.codex_live.EndpointUnavailable.DEFAULT, phase.codex_live.EndpointUnavailable.MISSING)
        refusal = codex_queue.QueueOwnerUnavailable('Queue holders: pid 4242, pid 4243.')
        args = dict(agent_session_id=self.codex_pin['id'], message='fixture')
        live = {'id': self.codex_sid, 'cwd': self.root, 'status': 'idle'}
        with (patch.object(broker, 'DEFAULT_SOCKET', Path(self.running.path)),
              patch.object(phase.codex_live.Client, 'connect', side_effect=endpoint),
              patch.object(phase.codex_live, 'exact_saved_workspace', return_value=(None, None)),
              patch.object(codex_queue, 'check_route', side_effect=refusal),
              patch.object(codex_queue, 'enqueue') as enqueue):
            with patch.object(sessions, 'resolve_codex_peer', side_effect=ValueError):
                refused = self.call('bridge_message_send', **args, idempotency_key='conflict-mcp-offline')
            self.assertEqual(refused['error']['message'], str(refusal))
            self.assertEqual(self.engine.rows(), [])
            with patch.object(sessions, 'resolve_codex_peer', return_value=live):
                accepted = self.data('bridge_message_send', **args, idempotency_key='conflict-mcp-online')
                self.engine.tick()
            operation = self.data('bridge_operation_get', operation_id=accepted['operation_id'])
            self.assertEqual(operation['backend_message_state'], 'pending')
            self.assertEqual(operation['failure']['message'], str(refusal))
            stored = self.engine.operation(accepted['operation_id'], 'test-owner')
            self.assertEqual(stored['failure']['message'], refusal.SUMMARY)
            with patch('sys.stdin', io.StringIO('fixture')), patch('sys.stdout', io.StringIO()) as out:
                code = bridge.main(['--socket', self.running.path, 'send', '--project', self.project,
                                    '--from', self.codex_pin['sender'], '--to', 'codex-worker',
                                    '--key', 'conflict-cli', '--file', '-'])
            enqueue.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())['error'], str(refusal))

    def test_missing_queue_workspace_reaches_mcp_preflight_and_delivery(self):
        endpoint = phase.codex_live.EndpointUnavailable
        refusal = codex_queue.missing_workspace(self.root)
        args = dict(agent_session_id=self.codex_pin['id'], message='fixture')
        live = {'id': self.codex_sid, 'cwd': self.root, 'status': 'idle'}
        with (patch.object(broker, 'DEFAULT_SOCKET', Path(self.running.path)),
              patch.object(phase.codex_live.Client, 'connect', side_effect=endpoint(endpoint.DEFAULT, endpoint.REFUSED)),
              patch.object(phase.codex_live, 'exact_saved_workspace', return_value=(None, None)),
              patch.object(codex_queue, 'check_route', side_effect=refusal),
              patch.object(codex_queue, 'enqueue') as enqueue):
            with patch.object(sessions, 'resolve_codex_peer', side_effect=ValueError):
                refused = self.call('bridge_message_send', **args, idempotency_key='missing-mcp-offline')
            self.assertEqual(refused['error']['message'], str(refusal))
            self.assertEqual(self.engine.rows(), [])
            with patch.object(sessions, 'resolve_codex_peer', return_value=live):
                accepted = self.data('bridge_message_send', **args, idempotency_key='missing-mcp-online')
                self.engine.tick()
            enqueue.assert_not_called()
        operation_id = accepted['operation_id']
        operation = self.data('bridge_operation_get', operation_id=operation_id)
        self.assertEqual((operation['backend_message_state'], operation['failure']['message']),
                         ('pending', str(refusal)))
        stored = self.engine.operation(operation_id, 'test-owner')
        self.assertEqual(stored['failure']['message'], refusal.summary)
        self.assertNotIn(self.root, stored['failure']['message'])

    def idle_claude(self, *, cwd=None, archived=False, host='local_a1b2c3d4'):
        self.fake_claude(pid=dead_pid(), listen=False,
                         extra={'entrypoint': 'claude-desktop', 'hostSessionId': host})
        desktop_dir = Path(self.root) / 'desktop'
        desktop_dir.mkdir(exist_ok=True)
        (desktop_dir / f'{host}.json').write_text(json.dumps({
            'sessionId': host, 'cliSessionId': self.claude_sid,
            'isArchived': archived, 'cwd': cwd or self.root,
        }))
        return host, {'CLAUDE_CONFIG_DIR': self.root, 'CLAUDE_DESKTOP_SESSIONS_DIR': str(desktop_dir)}

    @contextmanager
    def offline_codex(self, *, cwd=None, archived=False, default_broker=True):
        state = Path(self.root) / 'state_5.sqlite'
        with closing(sqlite3.connect(state)) as database, database:
            database.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER)')
            database.execute('INSERT INTO threads VALUES (?, ?, ?)', (self.codex_sid, cwd or self.root, archived))
        queue = state.with_name('queue_1.sqlite')
        queue.touch()
        endpoint = phase.codex_live.EndpointUnavailable
        broker_path = self.running.path if default_broker else self.root + '/other.sock'
        with (patch.dict(os.environ, {'CODEX_HOME': self.root}),
              patch.object(broker, 'DEFAULT_SOCKET', Path(broker_path)),
              patch.object(sessions, 'resolve_codex_peer', side_effect=sessions.DiscoveryError),
              patch.object(phase.codex_live.Client, 'connect', side_effect=endpoint(endpoint.DEFAULT, endpoint.MISSING)),
              patch.object(codex_queue, 'default_stores', return_value=(state, queue)),
              patch.object(codex_queue, 'verify_signature'),
              patch.object(codex_queue.codex_preview, 'state_path', return_value=state),
              patch.object(codex_queue, 'desktop_queue_owner', return_value=123),
              patch.object(codex_queue, 'enqueue', return_value=str(uuid.uuid4())) as enqueue,
              patch('codex_wake_open.request_queued_wake') as wake):
            yield enqueue, wake

    def send_fixture(self, session, key):
        return self.call('bridge_message_send', agent_session_id=session['id'],
                         message='MCP send fixture', idempotency_key=key)

    def test_idle_closed_claude_wakes_once_and_pushes_once(self):
        host, env = self.idle_claude()

        def open_wake(url, timeout=10):
            self.assertEqual(url, f'claude://claude.ai/epitaxy/{host}')
            self.assertEqual(self.running.call('ping')['messages'], 1)
            self.fake_claude(extra={'entrypoint': 'claude-desktop', 'hostSessionId': host})

        with patch.dict(os.environ, env), patch.object(sessions, 'open_url_background', side_effect=open_wake) as opener:
            self.assertEqual(self.backend.availability(self.claude_pin), 'unavailable')
            accepted = self.send_fixture(self.claude_pin, 'idle-claude-mcp-01')
            self.assertTrue(accepted['ok'], accepted)
            opener.assert_not_called()
            self.assertEqual(self.running.call('ping')['messages'], 0)
            self.engine.tick()
            self.assertTrue(self.send_fixture(self.claude_pin, 'idle-claude-mcp-01')['data']['replayed'])
            self.engine.tick()
            opener.assert_called_once()
            operation = self.engine.operation(accepted['data']['operation_id'], 'test-owner')
            message = self.running.call('show', id=operation['backend_id'])
            self.assertEqual(message['state'], 'submitted')
            self.assertIn('after wake (woken)', message['note'])
            self.assertEqual(len(self.fakes[-1].received), 1)

    def test_uncertain_claude_wake_stays_pending_without_blocking_or_resend(self):
        _, env = self.idle_claude()
        timeout = subprocess.TimeoutExpired(['open', '-g'], 1)
        with (patch.dict(os.environ, env), patch.object(sessions, 'open_url_background', side_effect=timeout) as opener,
              patch.object(phase.delivery, 'push_frame') as push):
            accepted = self.send_fixture(self.claude_pin, 'uncertain-claude-wake')
            self.assertTrue(accepted['ok'], accepted)
            self.engine.tick()
            operation = self.engine.operation(accepted['data']['operation_id'], 'test-owner')
            self.assertEqual((operation['status'], operation['backend_message_state']), ('failed', 'pending'))
            self.assertIn('wake uncertain', operation['failure']['message'])
            message = self.running.call('show', id=operation['backend_id'])
            self.assertEqual((message['state'], message['wakes'][-1]['state']), ('pending', 'uncertain'))
            self.assertTrue(self.send_fixture(self.claude_pin, 'uncertain-claude-wake')['data']['replayed'])
            self.engine.tick()
            opener.assert_called_once()
            push.assert_not_called()
            self.assertTrue(self.send_fixture(self.claude_pin, 'new-claude-send-after-wake')['ok'])

    def test_offline_codex_queues_once(self):
        with self.offline_codex() as (enqueue, wake):
            self.assertEqual(self.backend.availability(self.codex_pin), 'unavailable')
            accepted = self.send_fixture(self.codex_pin, 'offline-codex-mcp-01')
            self.assertTrue(accepted['ok'], accepted)
            enqueue.assert_not_called()
            wake.assert_not_called()
            self.assertEqual(self.running.call('ping')['messages'], 0)
            self.engine.tick()
            self.assertTrue(self.send_fixture(self.codex_pin, 'offline-codex-mcp-01')['data']['replayed'])
            self.engine.tick()
            operation = self.engine.operation(accepted['data']['operation_id'], 'test-owner')
            message = self.running.call('show', id=operation['backend_id'])
            self.assertEqual(message['state'], 'submitted')
            self.assertEqual(message['note'], 'Codex Desktop queue accepted the Agent Bridge notification')
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.args[0], self.codex_sid)
            wake.assert_called_once_with(self.codex_sid)

    def test_offline_discovered_only_is_refused_without_post(self):
        found = dict(self.codex_pin, id=phase.opaque('as_'), alias='codex-ws',
                     workspace=self.config['workspaces']['codex-ws'], pinned=False, discovered=True,
                     route='unregistered', registered=False)
        with self.offline_codex() as (enqueue, wake), patch.object(self.backend, 'discover', return_value={found['id']: found}):
            refused = self.send_fixture(found, 'offline-discovered')
            self.assertEqual(refused['error']['code'], 'BACKEND_UNAVAILABLE')
            self.assertEqual(self.engine.rows(), [])
            self.assertEqual(self.running.call('ping')['messages'], 0)
            enqueue.assert_not_called()
            wake.assert_not_called()

    def test_codex_loses_queue_after_live_acceptance_and_post(self):
        live = {'id': self.codex_sid, 'cwd': self.root, 'status': 'idle'}
        with patch.object(sessions, 'resolve_codex_peer', return_value=live):
            accepted = self.send_fixture(self.codex_pin, 'codex-queue-lost-after-post')
        self.assertTrue(accepted['ok'], accepted)
        original_post = self.backend.post
        refusal = codex_queue.QueueOwnerUnavailable('The queue holder could not be verified.')
        with self.offline_codex() as (enqueue, wake):
            def post_then_lose_queue(op, session):
                message = original_post(op, session)
                owner.side_effect = refusal
                return message

            with (patch.object(codex_queue, 'desktop_queue_owner', return_value=123) as owner,
                  patch.object(self.backend, 'post', side_effect=post_then_lose_queue)):
                self.engine.tick()
            enqueue.assert_not_called()
            wake.assert_not_called()
        operation = self.data('bridge_operation_get', operation_id=accepted['data']['operation_id'])
        self.assertEqual((operation['status'], operation['backend_message_state']), ('failed', 'pending'))
        self.assertEqual(operation['failure']['message'], str(refusal))
        self.assertEqual(self.running.call('ping')['messages'], 1)

    def test_running_claude_without_inbox_is_refused_without_post(self):
        _, env = self.idle_claude()
        self.fake_claude(listen=False, extra={'messagingSocketPath': None})
        with patch.dict(os.environ, env), patch.object(sessions, 'open_url_background') as opener:
            refused = self.send_fixture(self.claude_pin, 'claude-inbox-disabled')
            self.assertEqual(refused['error']['code'], 'BACKEND_UNAVAILABLE')
            self.assertIn('cross-session inbox off', refused['error']['message'])
            self.assertEqual(self.engine.rows(), [])
            self.assertEqual(self.running.call('ping')['messages'], 0)
            opener.assert_not_called()

    def test_ambiguous_idle_claude_is_refused_without_post(self):
        _, env = self.idle_claude()
        self.idle_claude(host='local_a1b2c3d5')
        with patch.dict(os.environ, env), patch.object(sessions, 'open_url_background') as opener:
            refused = self.send_fixture(self.claude_pin, 'ambiguous-claude-wake')
            self.assertIn('not choosing one', refused['error']['message'])
            self.assertEqual(self.engine.rows(), [])
            self.assertEqual(self.running.call('ping')['messages'], 0)
            opener.assert_not_called()

    def test_offline_codex_refusal_preserves_reason_before_post(self):
        for options, reason in (({'archived': True}, phase.codex_live.ThreadArchived.MESSAGE),
                                ({'default_broker': False}, str(phase.codex_live.EndpointUnavailable(
                                    phase.codex_live.EndpointUnavailable.DEFAULT, phase.codex_live.EndpointUnavailable.MISSING)))):
            with self.subTest(options=options), self.offline_codex(**options) as (enqueue, wake):
                refused = self.send_fixture(self.codex_pin, 'offline-codex-refused')
                self.assertEqual(refused['error']['message'], reason)
                self.assertEqual(self.engine.rows(), [])
                self.assertEqual(self.running.call('ping')['messages'], 0)
                enqueue.assert_not_called()
                wake.assert_not_called()
            (Path(self.root) / 'state_5.sqlite').unlink()

    def test_saved_workspace_mismatch_is_route_binding_refusal(self):
        _, env = self.idle_claude(cwd=self.root + '/other')
        with (patch.dict(os.environ, env), patch.object(sessions, 'open_url_background') as opener,
              self.offline_codex(cwd=self.root + '/other') as (enqueue, wake)):
            for target in (self.claude_pin, self.codex_pin):
                with self.subTest(target=target['agent_type']):
                    refused = self.send_fixture(target, 'offline-wrong-workspace')
                    self.assertEqual(refused['error']['message'], phase.ROUTE_CHANGED_MESSAGE)
            self.assertEqual(self.engine.rows(), [])
            self.assertEqual(self.running.call('ping')['messages'], 0)
            opener.assert_not_called()
            enqueue.assert_not_called()
            wake.assert_not_called()

    def test_backend_check_failing_inside_push_after_post_closes_unknown_blocks_and_leaves_pending(self):
        fake = self.fake_claude()
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self.root}):
            accepted = self.data('bridge_message_send',
                                 agent_session_id=self.claude_pin['id'],
                                 message='Post succeeds then check fails in push',
                                 idempotency_key='check-fail-in-push-01')
            oid = accepted['operation_id']
            original_post = self.backend.post

            def post_then_exit_peer(op, session):
                msg = original_post(op, session)
                # Peer exits right after broker post, before Backend.push's self.check(session).
                for p in (Path(self.root) / 'sessions').glob('*.json'):
                    p.unlink()
                return msg

            with patch.object(self.backend, 'post', side_effect=post_then_exit_peer):
                self.engine.tick()

            # 1. The operation closes as unknown.
            op_view = self.data('bridge_operation_get', operation_id=oid)
            op_doc = self.engine.operation(oid, 'test-owner')
            self.assertEqual(op_view['status'], 'unknown')
            self.assertEqual(op_view['failure']['code'], 'DELIVERY_UNKNOWN')
            self.assertEqual(op_doc['stage'], 'push_intent')
            self.assertEqual(op_doc['backend_message_state'], 'pending')
            self.assertEqual(op_doc['error']['type'], 'DiscoveryError')
            self.assertIsNotNone(op_doc['backend_id'])

            # 2. Engine.blocked blocks the session (both directly and via SESSION_BUSY on a new send).
            destination = self.engine.destination(self.claude_pin)
            self.assertTrue(self.engine.blocked(self.engine.rows(), destination))
            self.assertTrue(self.engine.blocked(self.engine.rows(), destination, queued=True))
            busy = self.call('bridge_message_send',
                             agent_session_id=self.claude_pin['id'],
                             message='Second message while blocked',
                             idempotency_key='check-fail-in-push-02')
            self.assertEqual(busy['error']['code'], 'SESSION_BUSY')

            # 3. The broker message stays pending with no events or notes, and nothing reached the peer socket.
            stored = self.running.call('show', id=op_doc['backend_id'])
            self.assertEqual(stored['state'], 'pending')
            self.assertEqual(stored['events'], [])
            self.assertIsNone(stored['note'])
            self.assertEqual(stored['body'], 'Post succeeds then check fails in push')
            self.assertEqual(fake.received, [])

    def test_inbox_reply_replay_after_lost_response_pushes_pending_once_and_never_repushes_other_states(self):
        fake = self.fake_claude()
        desktop_dir = Path(self.root) / 'desktop'
        desktop_dir.mkdir()
        env = {
            'CLAUDE_CONFIG_DIR': self.root,
            'CLAUDE_DESKTOP_SESSIONS_DIR': str(desktop_dir),
        }
        with patch.dict(os.environ, env):
            parent = self.running.call('send', project=self.project, sender='claude-worker',
                                       recipient=self.claude_pin['sender'],
                                       body='Question from Claude peer', key='parent-for-reply-199')
            listing = self.data('bridge_inbox_list', agent_session_id=self.claude_pin['id'])
            handle = listing['messages'][0]['message_id']
            reply_args = {
                'message_id': handle,
                'message': 'Real pushed reply after lost response',
                'idempotency_key': 'real-inbox-reply-199',
            }

            # Step 1: First call commits inbox_reply in broker, but response to facade is lost before push.
            real_rpc = self.backend.rpc
            def lose_inbox_reply(op, **kwargs):
                res = real_rpc(op, **kwargs)
                if op == 'inbox_reply':
                    raise broker.BrokerResponseError('simulated lost inbox_reply rpc response')
                return res

            with patch.object(self.backend, 'rpc', side_effect=lose_inbox_reply):
                lost = self.call('bridge_message_reply', **reply_args)
            self.assertEqual(lost['error']['code'], 'DELIVERY_UNKNOWN')
            self.assertEqual(fake.received, [])
            parent_after_post = self.running.call('show', id=parent['id'])
            self.assertEqual(parent_after_post['state'], 'answered')
            reply_id = parent_after_post['note']
            reply_before_replay = self.running.call('show', id=reply_id)
            self.assertEqual(reply_before_replay['state'], 'pending')
            self.assertEqual(reply_before_replay['events'], [])

            # Step 2: Replay while peer socket is unreachable -> real push runs, fails pre-write,
            # records push_failed event and leaves reply pending.
            fake.close()
            Path(fake.sock).unlink()
            first_replay = self.data('bridge_message_reply', **reply_args)
            self.assertEqual(first_replay, {
                'message_id': handle,
                'delivery_state': 'pending',
                'replayed': True,
            })
            reply_after_failed_push = self.running.call('show', id=reply_id)
            self.assertEqual(reply_after_failed_push['state'], 'pending')
            self.assertEqual([e['state'] for e in reply_after_failed_push['events']], ['dispatching', 'pending'])
            self.assertEqual(reply_after_failed_push['events'][-1]['event'], 'push_failed')
            self.assertIn('push_failed: Claude messaging socket', reply_after_failed_push['note'])

            # Step 3: Replay again with live peer -> still-pending reply is pushed once through real socket.
            live_fake = self.fake_claude()
            second_replay = self.data('bridge_message_reply', **reply_args)
            self.assertEqual(second_replay, {
                'message_id': handle,
                'delivery_state': 'submitted',
                'replayed': True,
            })
            self.assertEqual(len(live_fake.received), 1)
            auth, user = live_fake.received[0]
            self.assertEqual(auth, {'type': 'auth', 'token': TOKEN})
            self.assertEqual((user['type'], user['session_id']), ('user', self.claude_sid))
            self.assertIn('Real pushed reply after lost response', user['message']['content'])
            reply_submitted = self.running.call('show', id=reply_id)
            self.assertEqual(reply_submitted['state'], 'submitted')
            self.assertEqual([e['state'] for e in reply_submitted['events']],
                             ['dispatching', 'pending', 'dispatching', 'submitted'])
            self.assertEqual(reply_submitted['events'][-1]['event'], 'pushed')

            # Step 4: Subsequent replays in non-pending states never re-push to the peer socket.
            third_replay = self.data('bridge_message_reply', **reply_args)
            self.assertEqual(third_replay, second_replay)
            self.assertEqual(len(live_fake.received), 1)

            for next_state, op_kwargs in (
                ('received', {'op': 'ack', 'id': reply_id}),
                ('in_progress', {'op': 'progress', 'id': reply_id, 'note': 'working'}),
                ('done', {'op': 'resolve', 'id': reply_id, 'state': 'done', 'note': 'completed'}),
            ):
                with self.subTest(state=next_state):
                    self.running.call(op_kwargs.pop('op'), **op_kwargs)
                    replayed = self.data('bridge_message_reply', **reply_args)
                    self.assertEqual(replayed, {
                        'message_id': handle,
                        'delivery_state': next_state,
                        'replayed': True,
                    })
                    self.assertEqual(len(live_fake.received), 1)

            # Also verify a real uncertain push (peer answers unexpected bytes on push socket)
            # is recorded as uncertain and never re-pushed on replay.
            parent2 = self.running.call('send', project=self.project, sender='claude-worker',
                                        recipient=self.claude_pin['sender'],
                                        body='Second question from Claude peer', key='parent-uncertain-199')
            listing2 = self.data('bridge_inbox_list', agent_session_id=self.claude_pin['id'])
            handle2 = next(m['message_id'] for m in listing2['messages']
                           if m['body_sha256'] == hashlib.sha256(parent2['body'].encode()).hexdigest())
            uncertain_args = {
                'message_id': handle2,
                'message': 'Reply that hits an uncertain peer write',
                'idempotency_key': 'real-inbox-reply-uncertain-199',
            }
            with patch.object(phase.delivery, 'push_frame',
                              return_value='Claude inbox did not close the push connection before the deadline') as pf:
                first_uncertain = self.data('bridge_message_reply', **uncertain_args)
                replay_uncertain = self.data('bridge_message_reply', **uncertain_args)
                self.assertEqual(pf.call_count, 1)
            self.assertEqual(first_uncertain, {
                'message_id': handle2,
                'delivery_state': 'uncertain',
                'replayed': False,
            })
            self.assertEqual(replay_uncertain, {
                'message_id': handle2,
                'delivery_state': 'uncertain',
                'replayed': True,
            })
            uncertain_reply_id = self.running.call('show', id=parent2['id'])['note']
            uncertain_stored = self.running.call('show', id=uncertain_reply_id)
            self.assertEqual(uncertain_stored['state'], 'uncertain')
            self.assertEqual([e['state'] for e in uncertain_stored['events']], ['dispatching', 'uncertain'])
            self.assertIn('did not close the push connection', uncertain_stored['note'])
            # Clean up blocker so subsequent tests or checks on claude-worker are unblocked.
            self.running.call('resolve', id=uncertain_reply_id, state='done', note='resolved after uncertain test')
            replay_after_resolve = self.data('bridge_message_reply', **uncertain_args)
            self.assertEqual(replay_after_resolve, {
                'message_id': handle2,
                'delivery_state': 'done',
                'replayed': True,
            })
            self.assertEqual(len(live_fake.received), 1)

            # And a reply that reaches terminal failed state (e.g. archived Codex thread) is never re-pushed on replay.
            parent_codex = self.running.call('send', project=self.project, sender='codex-worker',
                                             recipient=self.codex_pin['sender'],
                                             body='Question from archived Codex', key='parent-archived-199')
            listing_codex = self.data('bridge_inbox_list', agent_session_id=self.codex_pin['id'])
            handle_codex = next(m['message_id'] for m in listing_codex['messages']
                                if m['body_sha256'] == hashlib.sha256(parent_codex['body'].encode()).hexdigest())
            failed_args = {
                'message_id': handle_codex,
                'message': 'Reply to archived Codex thread',
                'idempotency_key': 'real-inbox-reply-failed-199',
            }
            with patch.object(phase.delivery.codex_live, 'exact_saved_workspace',
                              return_value=(Path(self.root), True)) as archived_check:
                first_failed = self.data('bridge_message_reply', **failed_args)
                replay_failed = self.data('bridge_message_reply', **failed_args)
                self.assertEqual(archived_check.call_count, 1)
            self.assertEqual(first_failed, {
                'message_id': handle_codex,
                'delivery_state': 'failed',
                'replayed': False,
            })
            self.assertEqual(replay_failed, {
                'message_id': handle_codex,
                'delivery_state': 'failed',
                'replayed': True,
            })
            failed_reply_id = self.running.call('show', id=parent_codex['id'])['note']
            failed_stored = self.running.call('show', id=failed_reply_id)
            self.assertEqual(failed_stored['state'], 'failed')
            self.assertEqual(failed_stored['reason'], phase.delivery.ARCHIVED_REASON)
            self.assertEqual([e['state'] for e in failed_stored['events']], ['dispatching', 'failed'])

    def test_successful_send_and_reply_through_real_push_asserting_broker_final_states(self):
        fake = self.fake_claude()
        codex_row = {'provider': 'codex', 'id': self.codex_sid, 'cwd': self.root, 'status': 'idle'}
        with FakeDesktop('idle') as desktop, \
                patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self.root, 'AGENT_BRIDGE_CODEX_IPC': desktop.path}), \
                patch.object(sessions, 'resolve_codex_peer', return_value=codex_row):
            # 1. Outbound MCP send to Claude peer + correlated reply retirement.
            sent_claude = self.data('bridge_message_send',
                                    agent_session_id=self.claude_pin['id'],
                                    message='Hello real Claude peer',
                                    idempotency_key='real-send-claude-01')
            self.engine.tick()
            claude_op = self.engine.operation(sent_claude['operation_id'], 'test-owner')
            claude_mid = claude_op['backend_id']
            self.assertEqual((claude_op['status'], claude_op['stage'], claude_op['backend_message_state']),
                             ('running', 'observing', 'submitted'))
            self.assertEqual(len(fake.received), 1)
            auth, user = fake.received[0]
            self.assertEqual(auth, {'type': 'auth', 'token': TOKEN})
            self.assertIn(claude_mid, user['message']['content'])
            self.assertIn('Hello real Claude peer', user['message']['content'])

            broker_claude_msg = self.running.call('show', id=claude_mid)
            self.assertEqual(broker_claude_msg['state'], 'submitted')
            self.assertEqual([e['state'] for e in broker_claude_msg['events']], ['dispatching', 'submitted'])
            self.assertEqual(broker_claude_msg['events'][-1]['event'], 'pushed')
            self.assertIn(f'pushed to Claude session {self.claude_sid}', broker_claude_msg['note'])

            claude_reply = self.running.call('reply', id=claude_mid,
                                             body='Correlated answer from Claude', key='claude-reply-01')
            self.assertEqual(self.running.call('show', id=claude_mid)['state'], 'answered')
            self.assertEqual(self.running.call('show', id=claude_reply['id'])['state'], 'pending')

            self.engine.tick()
            claude_result = self.data('bridge_operation_get', operation_id=sent_claude['operation_id'])
            self.assertEqual((claude_result['status'], claude_result['result']['text']),
                             ('succeeded', 'Correlated answer from Claude'))
            final_claude_parent = self.running.call('show', id=claude_mid)
            final_claude_reply = self.running.call('show', id=claude_reply['id'])
            self.assertEqual(final_claude_parent['state'], 'answered')
            self.assertEqual(final_claude_parent['note'], claude_reply['id'])
            self.assertEqual([e['state'] for e in final_claude_parent['events']],
                             ['dispatching', 'submitted', 'answered'])
            self.assertEqual(final_claude_reply['state'], 'done')
            self.assertEqual(final_claude_reply['note'], 'MCP result durably captured')
            self.assertEqual([e['state'] for e in final_claude_reply['events']], ['done'])

            # 2. Outbound MCP send to Codex peer over FakeDesktop IPC + correlated reply retirement.
            sent_codex = self.data('bridge_message_send',
                                   agent_session_id=self.codex_pin['id'],
                                   message='Hello real Codex peer',
                                   idempotency_key='real-send-codex-01')
            self.engine.tick()
            codex_op = self.engine.operation(sent_codex['operation_id'], 'test-owner')
            codex_mid = codex_op['backend_id']
            self.assertEqual((codex_op['status'], codex_op['stage'], codex_op['backend_message_state']),
                             ('running', 'observing', 'submitted'))
            start_calls = [c for c in desktop.calls if c.get('method') == 'thread-follower-start-turn']
            self.assertEqual(len(start_calls), 1)
            turn_req = start_calls[0]['params']['turnStart']['request']
            self.assertEqual((turn_req['threadId'], turn_req['clientUserMessageId']), (self.codex_sid, codex_mid))
            self.assertIn('Hello real Codex peer', turn_req['toolOutput']['output'])

            broker_codex_msg = self.running.call('show', id=codex_mid)
            self.assertEqual(broker_codex_msg['state'], 'submitted')
            self.assertEqual([e['state'] for e in broker_codex_msg['events']], ['dispatching', 'submitted'])
            self.assertEqual(broker_codex_msg['events'][-1]['event'], 'pushed')
            self.assertEqual(broker_codex_msg['note'], 'Codex idle tool output accepted by turn idle-turn')

            codex_reply = self.running.call('reply', id=codex_mid,
                                            body='Correlated answer from Codex', key='codex-reply-01')
            self.engine.tick()
            codex_result = self.data('bridge_operation_get', operation_id=sent_codex['operation_id'])
            self.assertEqual((codex_result['status'], codex_result['result']['text']),
                             ('succeeded', 'Correlated answer from Codex'))
            final_codex_parent = self.running.call('show', id=codex_mid)
            final_codex_reply = self.running.call('show', id=codex_reply['id'])
            self.assertEqual(final_codex_parent['state'], 'answered')
            self.assertEqual(final_codex_parent['note'], codex_reply['id'])
            self.assertEqual(final_codex_reply['state'], 'done')
            self.assertEqual(final_codex_reply['note'], 'MCP result durably captured')

            # 3. Inbound peer message -> MCP bridge_message_ack and bridge_message_reply through real Codex push.
            inbound = self.running.call('send', project=self.project, sender='codex-worker',
                                        recipient=self.codex_pin['sender'],
                                        body='Independent question from Codex', key='inbound-codex-01')
            inbox = self.data('bridge_inbox_list', agent_session_id=self.codex_pin['id'])
            handle = next(m['message_id'] for m in inbox['messages']
                          if m['body_sha256'] == hashlib.sha256(inbound['body'].encode()).hexdigest())
            ack = self.data('bridge_message_ack', message_id=handle)
            self.assertTrue(ack['acknowledged'])
            replied = self.data('bridge_message_reply',
                                message_id=handle,
                                message='MCP inbox reply pushed to Codex',
                                idempotency_key='inbound-codex-reply-01')
            self.assertEqual(replied, {
                'message_id': handle,
                'delivery_state': 'submitted',
                'replayed': False,
            })
            final_inbound = self.running.call('show', id=inbound['id'])
            self.assertEqual(final_inbound['state'], 'answered')
            self.assertEqual([e['state'] for e in final_inbound['events']], ['done', 'answered'])
            pushed_reply_id = final_inbound['note']
            final_pushed_reply = self.running.call('show', id=pushed_reply_id)
            self.assertEqual(final_pushed_reply['state'], 'submitted')
            self.assertEqual(final_pushed_reply['reply_to'], inbound['id'])
            self.assertEqual([e['state'] for e in final_pushed_reply['events']], ['dispatching', 'submitted'])
            self.assertEqual(final_pushed_reply['events'][-1]['event'], 'pushed')
            self.assertEqual(final_pushed_reply['note'], 'Codex idle tool output accepted by turn idle-turn')
            start_calls = [c for c in desktop.calls if c.get('method') == 'thread-follower-start-turn']
            self.assertEqual(len(start_calls), 2)
            self.assertIn('MCP inbox reply pushed to Codex',
                          start_calls[1]['params']['turnStart']['request']['toolOutput']['output'])


if __name__ == '__main__':
    unittest.main()
