"""MCP Events protocol and isolated broker tests; never use real credentials."""
import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import mcp_events as events
import mcp_phase1 as phase
import mcp_server
from support import RunningBroker


class MemoryKeys:
    def __init__(self):
        self.values = {}
    def put(self, account, value):
        self.values[account] = value
    def get(self, account):
        return self.values[account]
    def delete(self, account):
        self.values.pop(account, None)


class Callback:
    def __init__(self):
        self.calls = []
        self.code = 204
    def __call__(self, url, body, headers):
        value = json.loads(body)
        self.calls.append((url, body, headers))
        if value.get('type') == 'verification':
            return 200, json.dumps({'challenge': value['challenge']}).encode()
        return self.code, b''


class EventTests(unittest.TestCase):
    def setUp(self):
        self.running = RunningBroker(max_messages=64).__enter__()
        self.addCleanup(self.running.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory(prefix='ab-events-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = str(Path(self.temp.name).resolve())
        self.config = {'principal': 'test-owner', 'tunnel_id': 'test-private', 'writes_enabled': True,
            'scopes': ['bridge:read', 'bridge:session:create', 'bridge:message:send'],
            'state_dir': self.root + '/ledger', 'inbox_enabled': True, 'events_enabled': True,
            'workspaces': {'test': {'cwd': self.root, 'root': self.root,
                'project': 'test-project', 'ide': next(iter(phase.sessions.CLAUDE_IDES))}},
            'pinned_sessions': {alias: {'uuid': str(uuid.uuid4()), 'cwd': self.root,
                'project': 'test-project', 'route': alias, 'authorized_at': phase.stamp()}
                for alias in ('hq', 'hq2')}}
        self.engine = phase.Engine(self.config, phase.Backend(self.running.path), start=False)
        self.addCleanup(lambda: self.engine.close())
        self.keys, self.callback = MemoryKeys(), Callback()
        self.engine.events.close()
        self.service = events.EventService(self.engine, self.keys, self.callback, start=False)
        self.engine.events = self.service
        self.owned = list(phase.pinned_sessions(self.config).values())
        self.sid = self.owned[0]['id']
        for session in self.owned:
            for name in (session['route'], session['sender']):
                self.running.call('register', project='test-project', name=name,
                    cwd=self.root, transport='realtime')
        self.secret = 'whsec_' + base64.b64encode(b'k' * 32).decode()
        self.params = {'name': events.NAME, 'arguments': {'agent_session_id': self.sid},
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example/events', 'secret': self.secret}}
        self.sequence = 0

    def rpc(self, method, params=None):
        return mcp_server.handle({'jsonrpc': '2.0', 'id': 1, 'method': method,
            'params': {} if params is None else params}, self.running.path, self.engine)

    def subscribe(self):
        value = self.rpc('events/subscribe', self.params)
        self.assertIn('result', value, value)
        return value['result']

    def send(self, index=0):
        self.sequence += 1
        session = self.owned[index]
        message = self.running.call('send', project='test-project', sender=session['route'],
            recipient=session['sender'], body='private body must stay out of webhook', key='event-' + str(self.sequence))
        entry = self.engine.inbox_routes(self.engine.principal, session['id'])[0]
        event = {'type': 'message', 'instance': self.running.instance, 'message': message}
        return entry, event

    def applications(self):
        return [json.loads(body) for _, body, _ in self.callback.calls if 'eventId' in json.loads(body)]

    def test_default_off_and_scope(self):
        self.assertIn('events', self.rpc('initialize')['result']['capabilities'])
        self.engine.events = None
        self.assertNotIn('events', self.rpc('initialize')['result']['capabilities'])
        self.assertEqual(self.rpc('events/list')['error']['code'], -32601)
        self.engine.events = self.service
        self.engine.scopes = frozenset()
        self.assertEqual(self.rpc('events/list')['error']['code'], -32601)

    def test_catalog_and_subscribe_signature(self):
        self.assertEqual(self.rpc('events/list')['result']['events'], [events.CATALOG])
        result = self.subscribe()
        self.assertIsNone(result['cursor'])
        self.assertFalse(result['truncated'])
        _, body, headers = self.callback.calls[0]
        self.assertEqual(set(json.loads(body)), {'type', 'challenge'})
        signed = headers['webhook-id'].encode() + b'.' + headers['webhook-timestamp'].encode() + b'.' + body
        expected = 'v1,' + base64.b64encode(hmac.digest(b'k'*32, signed, 'sha256')).decode()
        self.assertEqual(headers['webhook-signature'], expected)
        self.assertEqual(headers['X-MCP-Subscription-Id'], result['id'])

    def test_repeat_subscribe_refresh_and_verification_cache(self):
        first = self.subscribe()
        second = self.subscribe()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.service.subscriptions), 1)
        self.assertEqual(len(self.callback.calls), 1)
        self.assertEqual(len(self.keys.values), 2)  # current and brief old rotation key

    def test_signed_metadata_only_arrival_no_ack_no_duplicate(self):
        self.subscribe()
        entry, event = self.send()
        self.service.arrival(entry, event)
        self.service.arrival(entry, event)
        values = self.applications()
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]['data'], {'agent_session_id': self.sid})
        self.assertEqual(values[0]['timestamp'], event['message']['created'])
        self.assertNotIn('private body', json.dumps(values))
        unread = self.engine.call('bridge_inbox_list', {})['data']['messages']
        self.assertEqual(len(unread), 1)
        raw = self.service.path.read_text()
        self.assertNotIn(self.secret, raw)
        self.assertNotIn(event['message']['id'], raw)
        self.assertNotIn('private body', raw)
        self.assertEqual(self.service.path.stat().st_mode & 0o777, 0o600)

    def test_filter_status_correlated_other_route_and_old_messages(self):
        old_entry, old_event = self.send()
        self.subscribe()
        self.service.arrival(old_entry, old_event)
        entry, event = self.send(1)
        self.service.arrival(entry, event)
        entry, event = self.send()
        self.service.arrival(entry, dict(event, type='status'))
        self.service.arrival(entry, dict(event, message=dict(event['message'], reply_to='parent')))
        self.assertEqual(self.applications(), [])

    def test_callback_failure_never_retries_or_acks(self):
        self.subscribe()
        self.callback.code = 503
        entry, event = self.send()
        self.service.arrival(entry, event)
        self.service.arrival(entry, event)
        self.assertEqual(len(self.applications()), 1)
        self.assertEqual(self.service.health()['callback_failures'], 1)
        self.assertEqual(len(self.engine.call('bridge_inbox_list', {})['data']['messages']), 1)

    def test_rotation_dual_signature(self):
        self.subscribe()
        self.params['delivery']['secret'] = 'whsec_' + base64.b64encode(b'n' * 32).decode()
        self.subscribe()
        self.service.arrival(*self.send())
        headers = self.callback.calls[-1][2]
        self.assertEqual(len(headers['webhook-signature'].split()), 2)

    def test_unsubscribe_identity_is_idempotent(self):
        result = self.subscribe()
        params = copy.deepcopy(self.params)
        params['delivery'].pop('secret')
        self.assertEqual(self.rpc('events/unsubscribe', params)['result'], {})
        self.assertEqual(self.rpc('events/unsubscribe', params)['result'], {})
        self.assertNotIn(result['id'], self.service.subscriptions)
        self.service.arrival(*self.send())
        self.assertEqual(self.applications(), [])
        self.assertEqual(self.keys.values, {})

    def test_restart_retains_lease_but_no_backlog_replay(self):
        result = self.subscribe()
        self.service.close()
        self.service = events.EventService(self.engine, self.keys, self.callback, start=False)
        self.engine.events = self.service
        self.assertIn(result['id'], self.service.subscriptions)
        self.assertEqual(self.applications(), [])
        self.service.arrival(*self.send())
        self.assertEqual(len(self.applications()), 1)

    def test_policy_change_revokes_and_owner_change_refuses(self):
        self.subscribe()
        with patch.object(self.engine, 'inbox_policy', return_value='changed'):
            replacement = events.EventService(self.engine, self.keys, self.callback, start=False)
            self.assertEqual(replacement.subscriptions, {})
            replacement.close()
        with patch.object(self.engine, 'inbox_binding', return_value='another-owner'):
            with self.assertRaises(ValueError):
                events.EventService(self.engine, self.keys, self.callback, start=False)

    def test_fail_closed_keychain_and_atomic_store(self):
        self.keys.put = Mock(side_effect=events.EventError(-32603, 'keychain_unavailable'))
        self.assertEqual(self.rpc('events/subscribe', self.params)['error']['code'], -32603)
        self.assertEqual(self.service.subscriptions, {})
        self.keys.put = lambda account, value: self.keys.values.update({account: value})
        with patch.object(self.service, 'save', side_effect=OSError('disk full')):
            self.assertEqual(self.rpc('events/subscribe', self.params)['error']['code'], -32603)
        self.assertEqual(self.service.subscriptions, {})
        self.assertEqual(self.keys.values, {})

    def test_route_replacement_and_broker_epoch_refuse_delivery(self):
        self.subscribe()
        entry, event = self.send()
        self.service.arrival(entry, dict(event, instance='old-epoch'))
        self.assertEqual(self.applications(), [])
        self.running.call('register', project='test-project', name=entry['route']['name'],
            cwd=self.root, transport='realtime', session=str(uuid.uuid4()))
        self.service.arrival(entry, event)
        self.assertEqual(self.applications(), [])

    def test_invalid_arguments_secrets_and_callback_challenge(self):
        for value in ('whsec_!', 'whsec_' + base64.b64encode(b'k'*23).decode(),
                      'whsec_' + base64.b64encode(b'k'*65).decode(), 'not-secret'):
            self.params['delivery']['secret'] = value
            self.assertEqual(self.rpc('events/subscribe', self.params)['error']['code'], -32602)
        self.params['delivery']['secret'] = self.secret
        for url in ('http://example.com', 'https://u:p@example.com', 'https://example.com/#fragment', 'https://example.com:42/', 'https://example.com/\r\ninjected'):
            self.params['delivery']['url'] = url
            self.assertEqual(self.rpc('events/subscribe', self.params)['error']['code'], -32602)
        self.params['delivery']['url'] = 'https://callback.example/events'
        self.callback.__class__ = type('BadChallenge', (Callback,), {'__call__': lambda *args: (200, b'{"challenge":"wrong"}')})
        self.assertEqual(self.rpc('events/subscribe', self.params)['error']['code'], -32015)
        self.assertEqual(self.service.subscriptions, {})

    def test_finite_ttl_and_expiration(self):
        self.params['ttlMs'] = None
        result = self.subscribe()
        sub = self.service.subscriptions[result['id']]
        self.assertLessEqual(sub['expires'] - time.time(), events.DEFAULT_TTL_MS / 1000)
        sub['expires'] = time.time() - 1
        self.service.arrival(*self.send())
        self.assertEqual(self.applications(), [])

    def test_real_broker_watch_delivers_and_stops(self):
        self.service.close()
        self.service = events.EventService(self.engine, self.keys, self.callback, start=True)
        self.engine.events = self.service
        self.subscribe()
        self.send()
        deadline = time.monotonic() + 4
        while not self.applications() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(len(self.applications()), 1)
        self.assertEqual(self.service.health()['watching_routes'], 1)
        self.service.close()
        self.assertFalse(self.service.thread.is_alive())
        self.assertFalse(self.service.delivery_thread.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread, _, _ in self.service.watchers.values()))

    def test_uninitialized_pin_skipped_and_discovered_after_registration(self):
        absent = self.owned[1]
        self.running.call('unregister', project='test-project', name=absent['sender'])
        self.service.close()
        self.service = events.EventService(self.engine, self.keys, self.callback, start=True)
        self.engine.events = self.service
        self.params['arguments'] = {}
        self.subscribe()
        self.assertEqual(self.service.health()['watching_routes'], 1)
        self.assertEqual(self.service.health()['uninitialized_agent_session_ids'], [absent['id']])
        self.running.call('register', project='test-project', name=absent['sender'], cwd=self.root, transport='realtime')
        deadline = time.monotonic() + 4
        while self.service.health()['watching_routes'] != 2 and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.service.health()['watching_routes'], 2)
        self.send(1)
        while not self.applications() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(len(self.applications()), 1)

    def test_unsubscribe_cleanup_failure_keeps_recoverable_refs(self):
        self.subscribe()
        params = copy.deepcopy(self.params)
        params['delivery'].pop('secret')
        original = self.keys.delete
        self.keys.delete = Mock(side_effect=events.EventError(-32603, 'keychain_unavailable'))
        self.assertEqual(self.rpc('events/unsubscribe', params)['result'], {})
        self.assertEqual(self.service.subscriptions, {})
        self.assertEqual(len(self.service.retired_keys), 1)
        self.assertEqual(len(json.loads(self.service.path.read_text())['retired_keys']), 1)
        self.keys.delete = original
        self.service.close()
        self.service = events.EventService(self.engine, self.keys, self.callback, start=False)
        self.engine.events = self.service
        self.assertEqual(self.service.retired_keys, [])
        self.assertEqual(self.keys.values, {})

    def test_policy_revocation_persists_pending_key_cleanup(self):
        self.subscribe()
        original = self.keys.delete
        self.keys.delete = Mock(side_effect=events.EventError(-32603, 'keychain_unavailable'))
        with patch.object(self.engine, 'inbox_policy', return_value='changed'):
            replacement = events.EventService(self.engine, self.keys, self.callback, start=False)
            replacement.close()
        saved = json.loads(self.service.path.read_text())
        self.assertEqual(saved['subscriptions'], {})
        self.assertEqual(len(saved['retired_keys']), 1)
        self.keys.delete = original
        replacement = events.EventService(self.engine, self.keys, self.callback, start=False)
        replacement.close()
        self.assertEqual(self.keys.values, {})

    def test_keychain_delay_cannot_deliver_after_expiry_or_stop(self):
        result = self.subscribe()
        original = self.keys.get
        def expired(account):
            self.service.subscriptions[result['id']]['expires'] = time.time() - 1
            return original(account)
        self.keys.get = expired
        self.service.arrival(*self.send())
        self.assertEqual(self.applications(), [])
        self.service.subscriptions[result['id']]['expires'] = time.time() + 60
        def stopped(account):
            self.service.stop.set()
            return original(account)
        self.keys.get = stopped
        self.service.arrival(*self.send())
        self.assertEqual(self.applications(), [])

    def test_constructor_failure_releases_owner_ledger(self):
        self.engine.close()
        with patch.object(events, 'EventService', side_effect=ValueError('bad metadata')):
            with self.assertRaises(ValueError):
                phase.Engine(self.config, phase.Backend(self.running.path), start=False)
        self.engine = phase.Engine(self.config, phase.Backend(self.running.path), start=False)
        self.service = self.engine.events

    def test_close_refuses_active_thread_and_preserves_engine_db(self):
        active = Mock()
        active.is_alive.return_value = True
        self.service.thread = active
        with self.assertRaises(RuntimeError):
            self.engine.close()
        self.assertEqual(self.engine.db.execute('SELECT 1').fetchone()[0], 1)
        active.join.assert_called_once()
        self.service.thread = None

    def test_refresh_final_commit_failure_preserves_live_rotation_keys(self):
        result = self.subscribe()
        self.subscribe()
        active = dict(self.service.subscriptions[result['id']])
        current, previous = active['key_ref'], active['previous_key_ref']
        original = self.service.save
        writes = 0
        def failing_final_commit():
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError('planted final commit failure')
            original()
        with patch.object(self.service, 'save', side_effect=failing_final_commit):
            self.assertEqual(self.rpc('events/subscribe', self.params)['error']['code'], -32603)
        self.assertEqual(self.service.subscriptions[result['id']], active)
        self.assertNotIn(previous, self.service.retired_keys)
        self.assertNotIn(current, self.service.retired_keys)
        self.assertEqual(len(self.service.retired_keys), 1)
        self.service.cleanup_keys()
        self.assertEqual(set(self.keys.values), {current, previous})
        self.service.arrival(*self.send())
        self.assertEqual(len(self.applications()), 1)

    def test_status_schema_allows_optional_events_health(self):
        payload = self.rpc('tools/call', {'name': 'bridge_status', 'arguments': {}})['result']['structuredContent']
        self.assertIn('events', payload['data'])
        self.assertTrue(payload['data']['capabilities']['events'])
        for tools in (phase.TOOLS, mcp_server.TOOLS):
            tool = next(tool for tool in tools if tool['name'] == 'bridge_status')
            properties = tool['outputSchema']['properties']['data']['properties']
            self.assertIn('events', properties)
            self.assertEqual(set(payload['data']['events']) - set(properties['events']['properties']), set())



class HTTPSGuardTests(unittest.TestCase):
    def test_private_mixed_and_nonpublic_dns_blocked_before_socket(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))
        for addresses in ([private], [public, private], []):
            with patch.object(events.socket, 'getaddrinfo', return_value=addresses), patch.object(events.socket, 'socket') as connect:
                with self.assertRaises(events.EventError):
                    events.HTTPSCallback()('https://callback.example/', b'{}', {})
                connect.assert_not_called()

    def test_connection_pins_validated_ip_preserves_sni_and_no_redirects(self):
        raw, tls, connection = Mock(), Mock(), Mock()
        response = Mock(status=302)
        response.read.return_value = b''
        connection.getresponse.return_value = response
        context = Mock()
        context.wrap_socket.return_value = tls
        with patch.object(events.socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]) as dns, \
             patch.object(events.socket, 'socket', return_value=raw), \
             patch.object(events.ssl, 'create_default_context', return_value=context), \
             patch.object(events.http.client, 'HTTPSConnection', return_value=connection):
            code, _ = events.HTTPSCallback()('https://callback.example/path?q=1', b'{}', {})
        self.assertEqual(code, 302)
        dns.assert_called_once()
        raw.connect.assert_called_once_with(('8.8.8.8', 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname='callback.example')
        connection.request.assert_called_once_with('POST', '/path?q=1', body=b'{}', headers={})


class KeychainBoundaryTests(unittest.TestCase):
    def test_delete_missing_is_idempotent_but_locked_keychain_is_failure(self):
        result = Mock(returncode=0, stdout='', stderr='security: SecKeychainItemDelete: The specified item could not be found in the keychain.')
        with patch.object(events.subprocess, 'run', return_value=result):
            events.Keychain().delete('test-disposable')
        result.stderr = 'security: SecKeychainItemDelete: User interaction is not allowed.'
        with patch.object(events.subprocess, 'run', return_value=result):
            with self.assertRaises(events.EventError):
                events.Keychain().delete('test-disposable')



if __name__ == '__main__':
    unittest.main()
