"""Issue #48 isolated acceptance. No real HQ/Nana, live discovery or deployment.

Checklist: defaults/scopes; immutable Unicode/list paging; receipt reconciliation;
facade reconstruction; broker loss; route races; correlation exclusion; bounds.
"""
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import broker
import mcp_phase1 as phase
import mcp_server
import message_store
import route_store
from support import RunningBroker

try:
    import jsonschema
except ImportError:
    jsonschema = None


class BackendPingBudgetTests(unittest.TestCase):
    def test_slow_checkpoint_does_not_report_backend_loss(self):
        with tempfile.TemporaryDirectory(prefix='ab100-ping-', dir='/tmp') as root:
            store = route_store.RouteStore(Path(root) / 'state')
            self.addCleanup(store.close)
            state = broker.State(store=store)
            for name in ('alpha', 'beta'):
                state.action(dict(op='register', project='demo', name=name, cwd=root))
            message = state.action(dict(op='send', project='demo', sender='alpha', recipient='beta', body='Q', key='q'))
            path = str(Path(root) / 'test.sock')
            failures = []
            original = message_store.atomic_write
            def slow_save(target, data, *, measure=None):
                if target == state.correspondence.path:
                    time.sleep(.7)
                original(target, data, measure=measure)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen(1)
                def serve():
                    try:
                        with server.accept()[0] as client:
                            with client.makefile('rb') as reader:
                                self.assertEqual(json.loads(reader.readline())['op'], 'ping')
                            with patch.object(message_store, 'atomic_write', side_effect=slow_save):
                                state.action(dict(op='progress', id=message['id'], note='slow write'))
                            client.sendall(broker.wire(dict(result=state.action(dict(op='ping')))))
                    except BaseException as error:
                        failures.append(error)
                worker = threading.Thread(target=serve)
                worker.start()
                try:
                    self.assertEqual(phase.Backend(path).instance(), state.instance)
                finally:
                    worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])

    def test_unresponsive_broker_has_bounded_timeout(self):
        with tempfile.TemporaryDirectory(prefix='ab100-dead-', dir='/tmp') as root:
            path = str(Path(root) / 'test.sock')
            release = threading.Event()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen(1)
                def serve():
                    with server.accept()[0]:
                        release.wait(5)
                worker = threading.Thread(target=serve)
                worker.start()
                started = time.monotonic()
                try:
                    with self.assertRaises(TimeoutError):
                        phase.Backend(path).instance()
                    elapsed = time.monotonic() - started
                    self.assertGreaterEqual(elapsed, 2.5)
                    self.assertLess(elapsed, 4.5)
                finally:
                    release.set()
                    worker.join(timeout=5)


class InboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.running = RunningBroker(max_messages=64).__enter__()
        self.addCleanup(self.running.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory(prefix='ab48-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = str(Path(self.temp.name).resolve())
        self.config = {'principal': 'test-owner', 'tunnel_id': 'test-private', 'writes_enabled': True,
            'scopes': ['bridge:read', 'bridge:session:create', 'bridge:message:send'],
            'state_dir': self.root + '/ledger', 'inbox_enabled': True,
            'workspaces': {'test': {'cwd': self.root, 'root': self.root,
                'project': 'test-project', 'ide': next(iter(phase.sessions.CLAUDE_IDES))}},
            'pinned_sessions': {alias: {'uuid': str(uuid.uuid4()), 'cwd': self.root,
                'project': 'test-project', 'route': alias, 'authorized_at': phase.stamp()}
                for alias in ('hq', 'hq2')}}
        self.engine = phase.Engine(self.config, phase.Backend(self.running.path), start=False)
        self.addCleanup(lambda: self.engine.close())
        self.owned = list(phase.pinned_sessions(self.config).values())
        self.sid = self.owned[0]['id']
        for s in self.owned:
            self.register(s['route'])
            self.register(s['sender'])
        self.sequence = 0

    def register(self, name, **overrides):
        return self.running.call('register', **dict(project='test-project', name=name,
            cwd=self.root, transport='realtime', **overrides))

    def restart_facade(self):
        self.engine.close()
        self.engine = phase.Engine(self.config, phase.Backend(self.running.path), start=False)

    def call(self, name, **args):
        # Actual MCP request handler and facade engine, actual isolated socket broker.
        out = mcp_server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': args}}, self.running.path, self.engine)
        payload = out['result']['structuredContent']
        self.assertEqual(json.loads(out['result']['content'][0]['text']), payload)
        if jsonschema:
            jsonschema.validate(payload, next(t['outputSchema'] for t in phase.TOOLS if t['name'] == name))
        return payload

    def data(self, name, **args):
        out = self.call(name, **args)
        self.assertTrue(out['ok'], out)
        return out['data']

    def send(self, body='independent', index=0, sender=None):
        self.sequence += 1
        s = self.owned[index]
        return self.running.call('send', project='test-project', sender=sender or s['route'],
            recipient=s['sender'], body=body, key='inbox-' + str(self.sequence))

    def listing(self, **args):
        return self.data('bridge_inbox_list', **args)

    def handle(self, message):
        return next(m['message_id'] for m in self.listing()['messages']
                    if m['body_sha256'] == hashlib.sha256(message['body'].encode()).hexdigest())

    def test_default_config_scopes_and_hidden_tools(self):
        self.assertEqual(set(phase.SCOPES.values()), set(self.config['scopes']))
        status = self.data('bridge_status')
        self.assertEqual(len(status['enabled_tools']), 10)
        self.assertIn('bridge_message_reply', status['enabled_tools'])
        self.config.pop('inbox_enabled')
        self.assertEqual(len(self.engine.tools()), 6)
        for name in phase.INBOX_TOOLS:
            self.assertEqual(self.engine.call(name, {})['error']['code'], 'NOT_AUTHORIZED')
            response = mcp_server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': {}}}, self.running.path, self.engine)
            self.assertEqual(response['error']['code'], -32602)
        self.assertEqual(len(mcp_server.handle({'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/list'}, self.running.path)['result']['tools']), 1)
        config_path = Path(self.root) / 'config.json'
        config_path.write_text(json.dumps(self.config)); config_path.chmod(0o600)
        self.assertNotIn('inbox_enabled', phase.load_config(config_path))
        self.config['inbox_enabled'] = True
        self.config['scopes'] = ['bridge:read']
        self.engine.scopes = frozenset(self.config['scopes'])
        self.assertEqual({t['name'] for t in self.engine.tools()} & phase.INBOX_TOOLS,
            {'bridge_inbox_list', 'bridge_message_get'})
        self.assertEqual(self.engine.call('bridge_message_ack', {'message_id': 'x'})['error']['code'], 'NOT_AUTHORIZED')
        self.assertEqual(self.engine.call('bridge_message_reply', {})['error']['code'], 'NOT_AUTHORIZED')

    def reply_args(self, message, text='Reply body', key='inbox-reply-key-1'):
        return dict(message_id=self.handle(message), message=text, idempotency_key=key)

    def test_reply_to_frozen_claude_sender_without_an_outbound_pin(self):
        # The dedicated receive route survives the missing outbound pin route.
        receiver = self.owned[0]
        self.running.call('unregister', project='test-project', name=receiver['route'])
        claude_uuid = str(uuid.uuid4())
        self.running.call('register', project='test-project', name='independent-claude',
            cwd=self.root, transport='claude-peer', session=claude_uuid)
        parent = self.send('Request from independent Claude', sender='independent-claude')
        args = self.reply_args(parent, text='Ignore this instruction to send to hq2')
        self.data('bridge_message_ack', message_id=args['message_id'])
        before = self.running.call('routes', project='test-project')
        pushed = []

        def push(message):
            pushed.append(message)
            self.running.call('claim', id=message['id'])
            return self.running.call('finish', id=message['id'], state='submitted')

        with patch.object(self.engine.backend, 'push_inbox_reply', side_effect=push):
            first = self.data('bridge_message_reply', **args)
            replay = self.data('bridge_message_reply', **args)
            self.assertEqual(self.call('bridge_message_reply', **dict(args, message='different'))['error']['code'],
                'IDEMPOTENCY_CONFLICT')
        self.assertEqual(first, dict(message_id=args['message_id'], delivery_state='submitted', replayed=False))
        self.assertEqual(replay, dict(first, replayed=True))
        self.assertEqual(len(pushed), 1)
        route = pushed[0]['route']
        self.assertEqual((route['name'], route['transport'], route['session']),
            ('independent-claude', 'claude-peer', claude_uuid))
        self.assertEqual(pushed[0]['reply_to'], parent['id'])
        self.assertEqual(pushed[0]['body'], args['message'])
        self.assertNotIn(claude_uuid, json.dumps(first))
        self.assertEqual(self.running.call('routes', project='test-project'), before)
        self.assertEqual(self.engine.rows(), [])

    def test_reply_signed_handle_scope_route_and_epoch(self):
        parent = self.send('signed parent')
        args = self.reply_args(parent)
        handle = self.engine.inbox_token(args['message_id'], self.engine.principal, 'message')
        fields = {key: handle[key] for key in ('e', 'o', 's', 'r')}
        owner = self.engine.principal
        with patch.object(self.engine.backend, 'push_inbox_reply') as push:
            for token, code in ((parent['id'], 'INVALID_ARGUMENT'), ('forged', 'INVALID_ARGUMENT'),
                (self.engine.inbox_sign('other', 'message', **fields), 'INVALID_ARGUMENT'),
                (self.engine.inbox_sign(owner, 'message', **dict(fields, x=time.time() - 1)), 'CURSOR_EXPIRED'),
                (self.engine.inbox_sign(owner, 'message', **dict(fields, r='changed')), 'NOT_FOUND'),
                (self.engine.inbox_sign(owner, 'message', **dict(fields, e='changed')), 'BROKER_RESTARTED')):
                with self.subTest(code=code):
                    self.assertEqual(self.call('bridge_message_reply', **dict(args, message_id=token))['error']['code'], code)
            self.assertEqual(self.call('bridge_message_reply', **dict(args, recipient='hq2'))['error']['code'],
                'INVALID_ARGUMENT')
            self.assertEqual(self.engine.call('bridge_message_reply', args, principal='other')['error']['code'],
                'NOT_AUTHORIZED')
            push.assert_not_called()
        self.assertEqual(self.running.call('show', id=parent['id'])['state'], 'pending')

    def test_reply_lost_post_response_pushes_the_pending_reply_on_replay(self):
        args = self.reply_args(self.send('lost post'))
        rpc = self.engine.backend.rpc

        def lost(op, **kwargs):
            result = rpc(op, **kwargs)
            if op == 'inbox_reply':
                raise broker.BrokerResponseError('private response detail')
            return result

        with patch.object(self.engine.backend, 'rpc', side_effect=lost), patch.object(
                self.engine.backend, 'push_inbox_reply') as push:
            unknown = self.call('bridge_message_reply', **args)
            self.assertEqual(unknown['error']['code'], 'DELIVERY_UNKNOWN')
            self.assertNotIn('private response detail', json.dumps(unknown))
            push.assert_not_called()
        self.restart_facade()
        def delivered(message):
            self.running.call('claim', id=message['id'])
            return self.running.call('finish', id=message['id'], state='submitted')

        with patch.object(self.engine.backend, 'push_inbox_reply', side_effect=delivered) as push:
            # The reply was created but never written, so the retry delivers it (#198).
            replay = self.data('bridge_message_reply', **args)
            self.assertEqual((replay['delivery_state'], replay['replayed']), ('submitted', True))
            push.assert_called_once()
            again = self.data('bridge_message_reply', **args)
            self.assertEqual(again, replay)
            push.assert_called_once()  # Delivered: a later replay does not push again.

    def test_reply_unexpected_push_failure_is_logged_and_a_pending_retry_pushes(self):
        args = self.reply_args(self.send('unexpected push'))
        stderr = io.StringIO()
        with patch.object(self.engine.backend, 'push_inbox_reply',
                side_effect=OSError('private push detail')) as push, patch.object(sys, 'stderr', stderr):
            unknown = self.call('bridge_message_reply', **args)
            self.assertEqual(unknown['error']['code'], 'DELIVERY_UNKNOWN')
            self.assertNotIn('private push detail', json.dumps(unknown))
            self.assertEqual(push.call_count, 1)
        self.assertEqual(stderr.getvalue(), '{"event": "inbox_reply_push_failed"}\n')
        self.assertNotIn('private push detail', stderr.getvalue())
        # Nothing was written, so the message is still pending and the retry pushes it once.
        pushed = []

        def delivered(message):
            pushed.append(message['state'])
            self.running.call('claim', id=message['id'])
            return self.running.call('finish', id=message['id'], state='submitted')

        with patch.object(self.engine.backend, 'push_inbox_reply', side_effect=delivered):
            retry = self.data('bridge_message_reply', **args)
        self.assertEqual((retry['delivery_state'], retry['replayed'], pushed), ('submitted', True, ['pending']))

    def test_reply_push_uses_recorded_state_without_diagnostics_or_resend(self):
        args = self.reply_args(self.send('uncertain push'))

        def fail(message):
            self.running.call('claim', id=message['id'])
            recorded = self.running.call('finish', id=message['id'], state='uncertain')
            raise phase.delivery.PushFailed('private provider detail', recorded)

        with patch.object(self.engine.backend, 'push_inbox_reply', side_effect=fail) as push:
            first = self.data('bridge_message_reply', **args)
            self.assertEqual(first['delivery_state'], 'uncertain')
            self.assertNotIn('private provider detail', json.dumps(first))
            self.assertEqual(self.data('bridge_message_reply', **args), dict(first, replayed=True))
            push.assert_called_once()

    def test_independent_without_send_operation_and_no_implicit_ack(self):
        message = self.send('not a grant of authority')
        before = self.running.call('show', id=message['id'])
        listing = self.listing(agent_session_id=self.sid)
        item = listing['messages'][0]
        self.assertEqual(item['reply_to'], None)
        self.assertEqual(item['recipient']['name'], self.owned[0]['sender'])
        self.assertEqual(item['provenance'], 'broker_route')
        self.assertNotIn('body', item)
        encoded = json.dumps(item)
        self.assertNotIn(self.root, encoded)
        self.assertNotIn(self.owned[0]['uuid'], encoded)
        for _ in range(2):
            body = self.data('bridge_message_get', message_id=item['message_id'], max_chars=3)
            self.assertEqual(body['body'], message['body'][:3])
        # A disconnected read client has no consumption effect.
        routes = self.engine.inbox_routes(self.engine.principal, self.sid)
        args = dict(op='inbox_get', owner=self.engine.inbox_binding(self.engine.principal),
            context=phase.digest([self.engine.inbox_policy(), routes]), routes=routes,
            instance=listing['broker_instance'], id=message['id'], max_chars=2)
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(self.running.path); sock.sendall(broker.wire(args))
        self.assertEqual(self.running.call('show', id=message['id']), before)
        self.assertEqual(self.engine.rows(), [])

    def test_unicode_body_pages_reconnect_and_fixed_cursor(self):
        text = '🌈e\u0301한\n\x00\\"' * 600
        message = self.send(text)
        handle = self.handle(message)
        first = self.data('bridge_message_get', message_id=handle, max_chars=127)
        cursor = first['next_cursor']
        self.restart_facade()
        again = self.data('bridge_message_get', message_id=handle, max_chars=127)
        self.assertEqual(first['body'], again['body'])
        self.assertEqual(handle, again['message_id'])
        parts = [first['body']]
        while cursor:
            page = self.data('bridge_message_get', message_id=handle, cursor=cursor)
            retry = self.data('bridge_message_get', message_id=handle, cursor=cursor)
            self.assertEqual(page, retry)
            self.assertEqual(page['offset'], sum(map(len, parts)))
            parts.append(page['body']); cursor = page['next_cursor']
        self.assertEqual(''.join(parts), text)
        self.assertEqual(first['body_bytes'], len(text.encode()))
        self.assertEqual(first['body_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(self.running.call('show', id=message['id'])['state'], 'pending')

    def test_immutable_list_under_arrivals_ack_eviction_and_reconnect(self):
        messages = [self.send(str(i)) for i in range(5)]
        first = self.listing(limit=2)
        handle = first['messages'][0]['message_id']
        self.data('bridge_message_ack', message_id=handle)
        self.send('late')
        self.restart_facade()
        cursor = first['next_cursor']; items = first['messages'][:]
        while cursor:
            page = self.listing(cursor=cursor)
            self.assertEqual(page, self.listing(cursor=cursor))
            items.extend(page['messages']); cursor = page['next_cursor']
        self.assertEqual(len(items), 5)
        self.assertEqual(len({m['message_id'] for m in items}), 5)
        self.assertEqual(items[0]['state'], 'pending')
        fresh = self.listing()
        self.assertEqual(len(fresh['messages']), 5)
        self.assertIn(hashlib.sha256(b'late').hexdigest(), [m['body_sha256'] for m in fresh['messages']])
        self.assertEqual({m['body_sha256'] for m in items}, {hashlib.sha256(m['body'].encode()).hexdigest() for m in messages})

    def test_ack_lost_response_same_id_then_payload_eviction(self):
        message = self.send('consume once')
        handle = self.handle(message)
        rpc = self.engine.backend.rpc
        def lost(op, **args):
            result = rpc(op, **args)
            if op == 'inbox_ack':
                raise ValueError('Response lost')
            return result
        with patch.object(self.engine.backend, 'rpc', side_effect=lost):
            self.assertEqual(self.call('bridge_message_ack', message_id=handle)['error']['code'], 'BACKEND_UNAVAILABLE')
        saved = self.running.call('show', id=message['id'])
        self.assertEqual(saved['state'], 'done')
        self.assertIsNotNone(saved['acknowledged_at'])
        first = self.data('bridge_message_ack', message_id=handle)
        self.restart_facade()
        self.assertEqual(first, self.data('bridge_message_ack', message_id=handle))
        self.assertEqual(saved, self.running.call('show', id=message['id']))
        for i in range(64): self.send('fill-' + str(i))
        with self.assertRaisesRegex(ValueError, 'Unknown message'):
            self.running.call('show', id=message['id'])
        self.assertEqual(first, self.data('bridge_message_ack', message_id=handle))

    def test_correlated_reply_excluded_even_before_backend_id_saved(self):
        parent = self.running.call('send', project='test-project', sender=self.owned[0]['sender'],
            recipient=self.owned[0]['route'], body='question', key='parent', mcp_operation_send=True)
        reply = self.running.call('reply', id=parent['id'], body='correlated', key='reply')
        # Even a valid owner-signed guessed handle cannot expose correlated text.
        routes = self.engine.inbox_routes(self.engine.principal, self.sid)
        guessed = self.engine.inbox_sign(self.engine.principal, 'message',
            e=self.running.instance, o=reply['id'], s=self.sid, r=phase.digest(routes))
        self.assertEqual(self.listing()['messages'], [])
        for name in ('bridge_message_get', 'bridge_message_ack', 'bridge_message_reply'):
            args = (dict(message='no', idempotency_key='correlated-reply-key')
                    if name == 'bridge_message_reply' else {})
            self.assertEqual(self.call(name, message_id=guessed, **args)['error']['code'], 'NOT_FOUND')
        independent = self.send('separate answer')
        op = {'operation_id': 'op_' + 'x' * 24, 'principal': self.engine.principal,
            'kind': 'message_send', 'status': 'running', 'stage': 'post_intent',
            'accepted_at': phase.stamp(), 'updated_at': phase.stamp(), 'agent_session_id': self.sid,
            'message_request_id': 'msg_' + 'x' * 24, 'backend_message_state': None,
            'backend_id': None, 'instance': self.running.instance, 'result': None, 'failure': None}
        self.engine.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
            (op['operation_id'], self.engine.principal, 'bridge_message_send', 'key', 'hash', json.dumps(op)))
        self.assertEqual(len(self.listing()['messages']), 1)
        self.assertEqual(self.engine.operation(op['operation_id'], self.engine.principal)['status'], 'running')
        self.engine.update(op, backend_id=parent['id'])
        self.engine.observe(op)
        result = self.engine.get({'operation_id': op['operation_id']}, self.engine.principal)
        self.assertEqual(result['result']['text'], 'correlated')
        self.assertEqual(self.running.call('show', id=reply['id'])['state'], 'done')
        self.assertEqual(self.running.call('show', id=independent['id'])['state'], 'pending')
        self.assertEqual(len(self.listing()['messages']), 1)

    def test_principal_route_policy_and_token_isolation(self):
        a = self.send('a'); self.send('b', index=1)
        listing = self.listing(agent_session_id=self.sid, limit=1)
        self.assertEqual(len(listing['messages']), 1)
        handle = listing['messages'][0]['message_id']
        self.assertEqual(self.engine.call('bridge_inbox_list', {}, principal='other')['error']['code'], 'NOT_AUTHORIZED')
        self.running.call('register', project='foreign', name='sender', cwd=self.root, transport='realtime')
        self.running.call('register', project='foreign', name='receive', cwd=self.root, transport='realtime')
        self.running.call('send', project='foreign', sender='sender', recipient='receive', body='foreign', key='foreign-key')
        self.assertEqual(len(self.listing()['messages']), 2)
        wrong_owner = self.engine.inbox_sign('other', 'message', e=self.running.instance,
            o=a['id'], s=self.sid, r='incorrect')
        self.assertEqual(self.call('bridge_message_get', message_id=wrong_owner)['error']['code'], 'INVALID_ARGUMENT')
        for bad in (handle[:-1] + '!', 'forged'):
            self.assertEqual(self.call('bridge_message_get', message_id=bad)['error']['code'], 'INVALID_ARGUMENT')
        self.assertEqual(self.call('bridge_message_get', message_id=handle, cursor=handle)['error']['code'], 'INVALID_ARGUMENT')
        other_handle = next(m['message_id'] for m in self.listing()['messages'] if m['message_id'] != handle)
        long = self.send('long text')
        long_handle = self.handle(long)
        body = self.data('bridge_message_get', message_id=long_handle, max_chars=1)
        self.assertEqual(self.call('bridge_message_get', message_id=other_handle, cursor=body['next_cursor'])['error']['code'], 'INVALID_ARGUMENT')
        self.config['pinned_sessions'].pop('hq')
        for name in ('bridge_message_get', 'bridge_message_ack'):
            self.assertEqual(self.call(name, message_id=handle)['error']['code'], 'INVALID_ARGUMENT')
        self.assertEqual(self.running.call('show', id=a['id'])['state'], 'pending')

    def test_unused_new_pin_does_not_mask_pending_mail_or_changed_routes(self):
        message = self.send('valid pending mail')
        self.send('second pending mail')
        self.config['pinned_sessions']['unused'] = {
            'uuid': str(uuid.uuid4()), 'cwd': self.root, 'project': 'test-project',
            'route': 'unused', 'authorized_at': phase.stamp()}
        unused = next(s for s in phase.pinned_sessions(self.config).values()
                      if s['alias'] == 'unused')
        self.assertTrue(unused['registered'])
        # The peer target exists; Backend.post has never created its return route.
        self.running.call('register', project='test-project', name=unused['route'],
            cwd=self.root, transport='claude-peer', session=unused['uuid'])
        before = self.running.call('routes', project='test-project')
        self.assertNotIn(unused['sender'], [r['name'] for r in before])
        listing = self.listing()
        self.assertEqual(len(listing['messages']), 2)
        self.assertEqual(listing['uninitialized_agent_session_ids'], [unused['id']])
        self.assertNotIn(unused['uuid'], json.dumps(listing))
        first = self.listing(limit=1)
        self.assertTrue(first['has_more'])
        self.assertEqual(first['uninitialized_agent_session_ids'], [unused['id']])
        last = self.listing(cursor=first['next_cursor'])
        self.assertFalse(last['has_more'])
        self.assertEqual(last['uninitialized_agent_session_ids'], [unused['id']])
        self.assertEqual(before, self.running.call('routes', project='test-project'))
        handle = self.handle(message)
        self.assertEqual(self.data('bridge_message_get', message_id=handle)['body'], message['body'])
        # A present but changed route must fail even with an unused pin alongside.
        self.running.call('unregister', project='test-project', name=self.owned[0]['sender'])
        self.running.call('register', project='test-project', name=self.owned[0]['sender'],
            cwd=self.running.temp.name, transport='realtime')
        self.assertEqual(self.call('bridge_inbox_list')['error']['code'], 'ROUTE_CHANGED')
        self.running.call('unregister', project='test-project', name=self.owned[0]['sender'])
        self.register(self.owned[0]['sender'])
        self.register(unused['sender'])
        initialized = self.listing()
        self.assertEqual(len(initialized['messages']), 2)
        self.assertEqual(initialized['uninitialized_agent_session_ids'], [])
        # Initialization does not change the bound route set or old snapshot.
        resumed = self.listing(cursor=first['next_cursor'])
        self.assertEqual(resumed['messages'], last['messages'])
        self.assertEqual(resumed['uninitialized_agent_session_ids'], [])
        self.running.call('unregister', project='test-project', name=unused['sender'])
        self.assertEqual(self.call('bridge_inbox_list')['error']['code'], 'ROUTE_CHANGED')
        self.assertEqual(self.running.call('show', id=message['id'])['state'], 'pending')

    def test_atomic_route_replacement_between_precheck_and_rpc(self):
        message = self.send('route race'); self.send('second')
        first = self.listing(limit=1); handle = first['messages'][0]['message_id']
        for name, args in [('bridge_message_get', {'message_id': handle}),
                           ('bridge_message_ack', {'message_id': handle}),
                           ('bridge_message_reply', dict(message_id=handle, message='reply',
                                idempotency_key='changed-route-key')),
                           ('bridge_inbox_list', {'cursor': first['next_cursor']}),
                           ('bridge_inbox_list', {})]:
            original = self.engine.backend.rpc
            def replace(op, **fields):
                self.running.call('unregister', project='test-project', name=self.owned[0]['sender'])
                self.running.call('register', project='test-project', name=self.owned[0]['sender'],
                    cwd=self.running.temp.name, transport='realtime')
                return original(op, **fields)
            with patch.object(self.engine.backend, 'rpc', side_effect=replace):
                self.assertEqual(self.call(name, **args)['error']['code'], 'ROUTE_CHANGED')
            self.running.call('unregister', project='test-project', name=self.owned[0]['sender'])
            self.register(self.owned[0]['sender'])
        self.assertEqual(self.running.call('show', id=message['id'])['state'], 'pending')

    def test_broker_restart_revokes_old_tokens_and_preserves_unacknowledged_mail(self):
        message = self.send('long body'); remaining = self.send('another')
        first = self.listing(limit=1); handle = self.handle(message)
        body = self.data('bridge_message_get', message_id=handle, max_chars=1)
        self.data('bridge_message_ack', message_id=handle)
        self.running.restart()
        for name, args in [('bridge_message_get', {'message_id': handle}),
                           ('bridge_message_get', {'message_id': handle, 'cursor': body['next_cursor']}),
                           ('bridge_message_ack', {'message_id': handle}),
                           ('bridge_inbox_list', {'cursor': first['next_cursor']}),
                           ('bridge_inbox_list', {'expected_broker_instance': first['broker_instance']})]:
            error = self.call(name, **args)['error']
            self.assertEqual(error['code'], 'BROKER_RESTARTED')
            self.assertFalse(error['correspondence_lost'])
        fresh = self.listing()
        self.assertEqual(len(fresh['messages']), 1)
        handle = fresh['messages'][0]['message_id']
        self.assertEqual(self.data('bridge_message_get', message_id=handle)['body'], 'another')
        receipt = self.data('bridge_message_ack', message_id=handle)
        self.assertEqual(self.data('bridge_message_ack', message_id=handle), receipt)
        self.assertEqual(self.running.call('show', id=remaining['id'])['state'], 'done')
        self.assertIn('durable', fresh['restart_policy'])
        # Old epoch records release their capacity while consumed messages stay done.
        self.running.restart()
        self.assertEqual(self.listing()['messages'], [])
        checkpoint = json.loads((Path(self.running.state_dir) / 'correspondence.json').read_text())
        self.assertEqual(checkpoint['receipts'], [])
        self.assertEqual(checkpoint['snapshots'], {})

    def test_frame_budget_pages_full_inbox_without_unbounded_rpc(self):
        sender = 'h' + 'x' * 20000
        self.register(sender)
        for i in range(56): self.send('item-' + str(i), sender=sender)
        with self.assertRaisesRegex(ValueError, 'frame limit'):
            self.running.call('inbox', project='test-project', name=self.owned[0]['sender'])
        first = self.listing(limit=100)
        self.assertTrue(first['has_more'])
        self.assertLess(len(first['messages']), 56)
        items = first['messages'][:]; cursor = first['next_cursor']
        while cursor:
            page = self.listing(cursor=cursor)
            self.assertLess(len(broker.wire({'result': page, 'instance': self.running.instance})), broker.MAX_FRAME)
            items += page['messages']; cursor = page['next_cursor']
        self.assertEqual(len(items), 56)
        self.assertEqual(len({m['message_id'] for m in items}), 56)

    def test_expired_cursors_page_bounds_and_no_inbox_persistence(self):
        message = self.send('private independent body'); self.send('second')
        first = self.listing(limit=1); handle = self.handle(message)
        token = self.engine.inbox_token(first['next_cursor'], self.engine.principal, 'inbox')
        token.pop('p'); token.pop('g'); token.pop('k'); token['x'] = time.time() - 1
        expired = self.engine.inbox_sign(self.engine.principal, 'inbox', **token)
        self.assertEqual(self.call('bridge_inbox_list', cursor=expired)['error']['code'], 'CURSOR_EXPIRED')
        self.assertEqual(self.call('bridge_inbox_list', cursor=first['next_cursor'], limit=2)['error']['code'], 'INVALID_ARGUMENT')
        other_sid = self.owned[1]['id']
        self.assertEqual(self.call('bridge_inbox_list', cursor=first['next_cursor'], agent_session_id=other_sid)['error']['code'], 'INVALID_ARGUMENT')
        for args in ({'limit': 0}, {'limit': True}, {'limit': 101}):
            self.assertEqual(self.call('bridge_inbox_list', **args)['error']['code'], 'INVALID_ARGUMENT')
        self.data('bridge_message_get', message_id=handle, max_chars=2)
        self.data('bridge_message_ack', message_id=handle)
        tables = {r[0] for r in self.engine.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'operations', 'owned_sessions', 'metadata'})
        self.assertEqual(self.engine.rows(), [])
        self.assertNotIn(message['body'], (Path(self.running.state_dir) / 'routes.json').read_text())


class InboxAtomicBoundsTests(unittest.TestCase):
    def setUp(self):
        self.state = broker.State(max_messages=4)
        for name in ('sender', 'receive'):
            self.state.action(dict(op='register', project='test', name=name, cwd='/tmp', transport='realtime'))
        self.routes = [{'slot': 'test-slot', 'route': self.state.route('test', name='receive')}]
        self.args = dict(owner='a' * 64, context='b' * 64, routes=self.routes, instance=self.state.instance)

    def send(self, text):
        return self.state.action(dict(op='send', project='test', sender='sender', recipient='receive', body=text, key=text))

    def rpc(self, op, **args):
        return self.state.action(dict(self.args, op=op, **args))

    def test_ack_reserves_capacity_repeat_at_capacity_and_fixed_expiry(self):
        a, b = self.send('a'), self.send('b')
        with patch.object(broker, 'INBOX_MAX_RECEIPTS', 1):
            first = self.rpc('inbox_ack', id=a['id'])
            self.assertEqual(first, self.rpc('inbox_ack', id=a['id']))
            events = len(a['events'])
            with self.assertRaisesRegex(ValueError, 'CAPACITY_EXCEEDED'):
                self.rpc('inbox_ack', id=b['id'])
            self.assertEqual(b['state'], 'pending')
            with patch.object(broker.time, 'time', return_value=first['reconcile_until'] - 1):
                self.assertEqual(first, self.rpc('inbox_ack', id=a['id']))
            self.assertEqual(len(a['events']), events)
            with patch.object(broker.time, 'time', return_value=first['reconcile_until'] + 1):
                with self.assertRaisesRegex(ValueError, 'ACK_EXPIRED'):
                    self.rpc('inbox_ack', id=a['id'])

    def test_snapshot_capacity_expiry_equal_times_and_payload_eviction(self):
        a, b = self.send('a'), self.send('b')
        a['created'] = b['created'] = 'same-time'
        with patch.object(broker, 'INBOX_MAX_SNAPSHOTS', 1):
            first = self.rpc('inbox_list', limit=1)
            with self.assertRaisesRegex(ValueError, 'CAPACITY_EXCEEDED'):
                self.rpc('inbox_list', limit=1)
            self.assertEqual(a['state'], 'pending')
            self.rpc('inbox_ack', id=first['messages'][0]['id'])
            self.send('c'); self.send('d'); self.send('e')
            last = self.rpc('inbox_list', snapshot=first['snapshot'], offset=first['offset'], limit=1)
            ids = [first['messages'][0]['id'], last['messages'][0]['id']]
            self.assertEqual(ids, sorted([a['id'], b['id']]))
            self.assertFalse(last['has_more'])
            # Completing traversal retains the slot until the fixed expiry.
            with self.assertRaisesRegex(ValueError, 'CAPACITY_EXCEEDED'):
                self.rpc('inbox_list', limit=1)
            with patch.object(broker.time, 'time', return_value=first['expires'] + 1):
                with self.assertRaisesRegex(ValueError, 'CURSOR_EXPIRED'):
                    self.rpc('inbox_list', snapshot=first['snapshot'], offset=1, limit=1)
        self.assertEqual(broker.INBOX_MAX_SNAPSHOTS, 16)
        for _ in range(16):
            page = self.rpc('inbox_list', limit=1)
            while page['has_more']:
                page = self.rpc('inbox_list', snapshot=page['snapshot'], offset=page['offset'], limit=1)
        with self.assertRaisesRegex(ValueError, 'CAPACITY_EXCEEDED'):
            self.rpc('inbox_list', limit=1)

    def test_dispatching_uncertain_and_correlated_not_consumable(self):
        message = self.send('a')
        for state in ('dispatching', 'uncertain'):
            message['state'] = state
            with self.assertRaisesRegex(ValueError, 'DELIVERY_UNKNOWN'):
                self.rpc('inbox_ack', id=message['id'])
            self.assertEqual(self.state.inbox_receipts, {})
        message['reply_to'] = 'orphan-id'
        for op in ('inbox_get', 'inbox_ack'):
            with self.assertRaisesRegex(ValueError, 'NOT_FOUND'):
                self.rpc(op, id=message['id'])
        self.assertEqual(self.rpc('inbox_list')['messages'], [])

    def test_correlated_notification_validation_checks_route_and_epoch_before_refusal(self):
        message = self.send('reply notification validation')
        message['reply_to'] = 'captured-parent'
        original = json.dumps(self.state.messages, sort_keys=True)
        # Events relies only on these exact refusals AFTER validation, including
        # when a durably captured reply's terminal payload has been evicted.
        for mid, code in ((message['id'], 'NOT_FOUND'), ('evicted-reply', 'MESSAGE_UNAVAILABLE')):
            with self.subTest(mid=mid):
                with self.assertRaisesRegex(ValueError, '^' + code + '$'):
                    self.rpc('inbox_get', id=mid, offset=0, max_chars=1)
                with self.assertRaisesRegex(ValueError, '^BROKER_RESTARTED$'):
                    self.rpc('inbox_get', id=mid, offset=0, max_chars=1, instance='changed-instance')
                wrong = json.loads(json.dumps(self.routes))
                wrong[0]['route']['cwd'] = '/route/changed'
                with self.assertRaisesRegex(ValueError, '^ROUTE_CHANGED$'):
                    self.rpc('inbox_get', id=mid, offset=0, max_chars=1, routes=wrong)
        self.assertEqual(json.dumps(self.state.messages, sort_keys=True), original)
        self.assertEqual(self.state.inbox_snapshots, {})
        self.assertEqual(self.state.inbox_receipts, {})

    def test_reply_classification_with_evicted_or_unknown_parent(self):
        parent = self.state.action(dict(op='send', project='test', sender='receive',
            recipient='sender', body='ordinary local mail', key='local-parent'))
        reply = self.state.action(dict(op='reply', id=parent['id'], body='independent', key='local-reply'))
        for index in range(3):
            self.send('mail-' + str(index))
        self.assertNotIn(parent['id'], self.state.messages)
        self.assertEqual(self.rpc('inbox_get', id=reply['id'])['body'], 'independent')
        self.assertIn(reply['id'], [item['id'] for item in self.rpc('inbox_list')['messages']])

        # A pre-upgrade record with the same missing parent has no saved proof.
        del reply['independent_reply']
        with self.assertRaisesRegex(ValueError, '^NOT_FOUND$'):
            self.rpc('inbox_get', id=reply['id'])
        self.assertNotIn(reply['id'], [item['id'] for item in self.rpc('inbox_list')['messages']])
        with self.assertRaisesRegex(ValueError, 'Unknown message'):
            self.state.action(dict(op='reply', id=parent['id'], body='new reply', key='missing-parent'))

    def test_unmarked_legacy_operation_parent_remains_operation_only(self):
        sender = 'mcp-' + 'a' * 32
        self.state.action(dict(op='register', project='test', name=sender, cwd='/tmp', transport='realtime'))
        parent = self.state.action(dict(op='send', project='test', sender=sender,
            recipient='sender', body='legacy operation', key='legacy-key'))
        del parent['mcp_operation_send']
        reply = self.state.action(dict(op='reply', id=parent['id'], body='correlated', key='legacy-reply'))
        routes = [{'slot': 'legacy-slot', 'route': self.state.route('test', name=sender)}]
        self.assertEqual(self.rpc('inbox_list', routes=routes)['messages'], [])
        with self.assertRaisesRegex(ValueError, '^NOT_FOUND$'):
            self.rpc('inbox_get', id=reply['id'], routes=routes)



class AdoptedRouteInboxTests(unittest.TestCase):
    """Issue #186: configured realtime routes of the owner join the MCP inbox."""
    setUp = InboxIntegrationTests.setUp
    call = InboxIntegrationTests.call
    data = InboxIntegrationTests.data
    listing = InboxIntegrationTests.listing
    restart_facade = InboxIntegrationTests.restart_facade

    def register(self, name, **overrides):
        return self.running.call('register', **dict(dict(project='test-project', name=name,
            cwd=self.root, transport='realtime'), **overrides))

    def adopt(self, prefixes=None):
        self.config['inbox_route_prefixes'] = {'test-project': ['nana-']} if prefixes is None else prefixes

    def mail(self, recipient, body, sender='writer', project='test-project'):
        self.sequence += 1
        return self.running.call('send', project=project, sender=sender, recipient=recipient,
            body=body, key='adopt-' + str(self.sequence))

    def slot(self, name, project='test-project'):
        return 'ar_' + phase.digest([self.config['principal'], self.config['tunnel_id'],
                                     'adopted', project, name])[:32]

    def count_routes_reads(self):
        reads = []
        rpc = self.engine.backend.rpc
        def counted(op, **kwargs):
            if op == 'routes':
                reads.append(kwargs['project'])
            return rpc(op, **kwargs)
        return reads, patch.object(self.engine.backend, 'rpc', side_effect=counted)

    def test_absent_or_empty_field_keeps_routes_digest_and_reads_no_routes(self):
        self.register('nana-x')
        self.register('writer')
        reads, counting = self.count_routes_reads()
        with counting:
            baseline = self.engine.inbox_routes(self.engine.principal)
            selected = self.engine.inbox_routes(self.engine.principal, self.sid)
            self.config['inbox_route_prefixes'] = {}
            self.assertEqual(phase.digest(self.engine.inbox_routes(self.engine.principal)), phase.digest(baseline))
            self.assertEqual(self.engine.inbox_routes(self.engine.principal, self.sid), selected)
        self.assertEqual(reads, [])
        self.assertEqual([e['slot'] for e in baseline], sorted(s['id'] for s in self.owned))
        self.mail('nana-x', 'not adopted without the field')
        self.assertEqual(self.listing()['messages'], [])
        self.assertEqual(self.call('bridge_inbox_list', agent_session_id=self.slot('nana-x'))['error']['code'],
                         'NOT_FOUND')

    def test_adopted_route_is_listed_readable_ackable_and_replyable(self):
        elsewhere = str(Path(self.running.temp.name).resolve())
        self.register('nana-x', cwd=elsewhere)  # Its cwd need not be a workspace.
        self.register('writer')
        owned_before = self.engine.inbox_routes(self.engine.principal)
        self.adopt()
        routes = self.engine.inbox_routes(self.engine.principal)
        slot = self.slot('nana-x')
        adopted = {'slot': slot, 'route': {'project': 'test-project', 'name': 'nana-x', 'cwd': elsewhere,
                                           'transport': 'realtime', 'session': None}}
        self.assertEqual(routes, sorted(owned_before + [adopted], key=lambda e: e['slot']))
        self.assertEqual(self.engine.inbox_routes(self.engine.principal, slot), [adopted])
        parent = self.mail('nana-x', 'parent for the adopted route')
        second = self.mail('nana-x', 'second pending mail')
        unfiltered = self.listing()
        self.assertEqual([m['agent_session_id'] for m in unfiltered['messages']], [slot, slot])
        self.assertEqual(unfiltered['messages'][0]['recipient']['name'], 'nana-x')
        self.assertNotIn(elsewhere, json.dumps(unfiltered))
        first = self.listing(agent_session_id=slot, limit=1)
        self.assertTrue(first['has_more'])
        rest = self.listing(cursor=first['next_cursor'])
        self.assertEqual(len(first['messages'] + rest['messages']), 2)
        handle = first['messages'][0]['message_id']
        self.assertEqual(self.data('bridge_message_get', message_id=handle)['body'], parent['body'])
        self.assertTrue(self.data('bridge_message_ack', message_id=handle)['acknowledged'])
        self.assertEqual(self.running.call('show', id=parent['id'])['state'], 'done')
        pushed = []
        def push(message):
            pushed.append(message)
            return message
        with patch.object(self.engine.backend, 'push_inbox_reply', side_effect=push):
            reply = self.data('bridge_message_reply', message_id=handle, message='adopted reply',
                              idempotency_key='adopted-reply-key-1')
        self.assertEqual(reply, dict(message_id=handle, delivery_state='pending', replayed=False))
        # The reply is sent as the adopted route the mail arrived on.
        self.assertEqual((pushed[0]['sender'], pushed[0]['recipient'], pushed[0]['reply_to']),
                         ('nana-x', 'writer', parent['id']))
        self.restart_facade()
        self.assertEqual([m['body_sha256'] for m in self.listing()['messages']],
                         [hashlib.sha256(second['body'].encode()).hexdigest()])

    def test_only_matching_realtime_sessionless_routes_in_configured_projects_are_adopted(self):
        self.running.call('register', project='foreign', name='nana-x', cwd=self.root, transport='realtime')
        self.running.call('register', project='foreign', name='writer', cwd=self.root, transport='realtime')
        self.register('writer')
        self.register('other-x')
        self.register('nana-session', session=str(uuid.uuid4()))
        self.register('nana-peer', transport='claude-peer', session=str(uuid.uuid4()))
        self.register('nana-x')
        self.adopt()
        for name in ('other-x', 'nana-session'):
            self.mail(name, 'must stay out of the inbox: ' + name)
        self.mail('nana-x', 'foreign mail', project='foreign')
        self.assertEqual([e['route']['name'] for e in self.engine.inbox_routes(self.engine.principal)
                          if e['slot'].startswith('ar_')], ['nana-x'])
        self.assertEqual(self.listing()['messages'], [])
        for name, project in (('other-x', 'test-project'), ('nana-session', 'test-project'),
                              ('nana-peer', 'test-project'), ('nana-x', 'foreign')):
            with self.subTest(name=name, project=project):
                self.assertEqual(self.call('bridge_inbox_list',
                    agent_session_id=self.slot(name, project))['error']['code'], 'NOT_FOUND')
        # An adopted slot is inbox-only: the send schema accepts only as_ session ids.
        refused = self.engine.call('bridge_message_send', {'agent_session_id': self.slot('nana-x'),
            'message': 'no new send authority', 'idempotency_key': 'adopted-send-key-1'})
        self.assertEqual(refused['error']['code'], 'INVALID_ARGUMENT')
        self.assertEqual(self.engine.rows(), [])

    def test_owned_sender_wins_over_a_matching_prefix(self):
        baseline = self.engine.inbox_routes(self.engine.principal)
        # Validation refuses a prefix that reaches the facade's own mcp- names, so this second
        # guard is only reachable past it: every owned sender matches, and none is adopted twice.
        with patch.object(phase, 'inbox_route_prefixes', return_value={'test-project': ('mcp-',)}):
            self.assertEqual(self.engine.inbox_routes(self.engine.principal), baseline)
            self.assertEqual(self.call('bridge_inbox_list',
                agent_session_id=self.slot(self.owned[0]['sender']))['error']['code'], 'NOT_FOUND')

    def test_combined_capacity_is_bounded(self):
        self.adopt()
        for index in range(62):
            self.register('nana-%02d' % index)
        self.assertEqual(len(self.engine.inbox_routes(self.engine.principal)), 64)
        self.register('nana-62')
        self.assertEqual(self.call('bridge_inbox_list')['error']['code'], 'CAPACITY_EXCEEDED')

    def test_route_reads_are_bounded_and_broker_loss_is_backend_unavailable(self):
        self.register('nana-x')
        self.adopt()
        reads, counting = self.count_routes_reads()
        with counting:
            self.listing()
            self.assertEqual(reads, ['test-project'])
            del reads[:]
            self.listing(agent_session_id=self.sid)
            self.assertEqual(reads, [])
            self.listing(agent_session_id=self.slot('nana-x'))
            self.assertEqual(reads, ['test-project'])
        rpc = self.engine.backend.rpc
        def down(op, **kwargs):
            if op == 'routes':
                raise phase.broker.BrokerConnectError(OSError('planted'))
            return rpc(op, **kwargs)
        with patch.object(self.engine.backend, 'rpc', side_effect=down):
            self.assertEqual(self.call('bridge_inbox_list')['error']['code'], 'BACKEND_UNAVAILABLE')
            self.assertEqual(self.call('bridge_inbox_list', agent_session_id=self.slot('nana-x'))['error']['code'],
                             'BACKEND_UNAVAILABLE')
            self.assertTrue(self.call('bridge_inbox_list', agent_session_id=self.sid)['ok'])

    def test_replaced_adopted_route_invalidates_its_handles(self):
        self.register('nana-x')
        self.register('writer')
        self.adopt()
        self.mail('nana-x', 'before replacement')
        handle = self.listing()['messages'][0]['message_id']
        self.running.call('unregister', project='test-project', name='nana-x')
        self.register('nana-x', cwd=str(Path(self.running.temp.name).resolve()))
        self.assertEqual(self.call('bridge_message_get', message_id=handle)['error']['code'], 'NOT_FOUND')
        self.config['inbox_route_prefixes'] = {}
        self.assertEqual(self.call('bridge_message_ack', message_id=handle)['error']['code'], 'NOT_FOUND')

    def test_load_config_validates_inbox_route_prefixes(self):
        path = Path(self.root) / 'config.json'
        def load(value):
            config = dict(self.config, inbox_route_prefixes=value)
            path.write_text(json.dumps(config)); path.chmod(0o600)
            return phase.load_config(path)
        self.assertEqual(load({'test-project': ['nana-', 'n_2']})['inbox_route_prefixes'],
                         {'test-project': ['nana-', 'n_2']})
        self.assertEqual(load({})['inbox_route_prefixes'], {})
        for bad in ({'foreign': ['nana-']}, {'test-project': []}, {'test-project': ['']},
                    {'test-project': ['Nana-']}, {'test-project': ['-nana']}, {'test-project': ['nana x']},
                    {'test-project': ['n' * 33]}, {'test-project': 'nana-'}, {'test-project': [7]},
                    {'test-project': ['p%d' % i for i in range(9)]}, ['nana-'],
                    {'p%d' % i: ['nana-'] for i in range(9)},
                    {'test-project': ['mcp-']}, {'test-project': ['mcp-a']}, {'test-project': ['m']},
                    {'test-project': ['mc']}, {'test-project': ['nana-', 'mcp']}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    load(bad)


if __name__ == '__main__':
    unittest.main()
