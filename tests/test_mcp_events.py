"""MCP Events protocol and isolated broker tests; never use real credentials."""
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
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

    def test_dual_event_capability_is_opt_in_and_keeps_legacy_negotiation(self):
        for version in mcp_server.PROTOCOL_VERSIONS:
            result = self.rpc('initialize', {'protocolVersion': version})['result']
            self.assertEqual(result['protocolVersion'], version)
            self.assertEqual(result['capabilities'], {'tools': {'listChanged': False},
                'events': {}, 'extensions': {'io.modelcontextprotocol/events': {}}})
        result = self.rpc('initialize', {'protocolVersion': '2026-07-28'})['result']
        self.assertEqual(result['protocolVersion'], '2025-11-25')
        self.engine.events = None
        try:
            self.assertEqual(self.rpc('initialize')['result']['capabilities'], {'tools': {'listChanged': False}})
        finally:
            self.engine.events = self.service
        self.assertEqual(len(self.engine.tools()), 8)

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
            try:
                import jsonschema
            except ImportError:
                pass
            else:
                jsonschema.validate(payload, tool['outputSchema'])


    def test_request_diagnostics_track_received_success_and_failed_native_calls(self):
        initial = self.service.health()['request_diagnostics']
        self.assertTrue(initial['started_at'].endswith('Z'))
        self.assertEqual(set(initial['methods']), set(events.EVENT_METHODS))
        self.assertTrue(all(slot['received_count'] == 0 for slot in initial['methods'].values()))
        self.rpc('events/list')
        self.rpc('events/subscribe', {})
        self.subscribe()
        params = copy.deepcopy(self.params)
        params['delivery'].pop('secret')
        self.rpc('events/unsubscribe', params)
        methods = self.service.health()['request_diagnostics']['methods']
        self.assertEqual((methods['events/list']['received_count'], methods['events/list']['accepted_count']), (1, 1))
        slot = methods['events/subscribe']
        self.assertEqual((slot['received_count'], slot['accepted_count'], slot['failed_count']), (2, 1, 1))
        self.assertEqual((slot['last_stage'], slot['last_outcome']), ('complete', 'accepted'))
        self.assertEqual(slot['last_failure']['reason'], 'invalid_event')
        self.assertTrue(slot['last_received_at'].endswith('Z'))
        self.assertEqual(methods['events/unsubscribe']['accepted_count'], 1)

    def test_failed_callback_stage_visible_without_retaining_exception_text(self):
        sensitive = 'private-url-whsec-keyref-message-id-cursor-DO-NOT-RETAIN'
        observed = []
        def failing_callback(*args):
            observed.append(self.service.health()['request_diagnostics']['methods']['events/subscribe'])
            raise RuntimeError(sensitive)
        self.service.callback = failing_callback
        result = self.rpc('events/subscribe', self.params)
        self.assertEqual(result['error']['code'], -32603)
        self.assertEqual(observed[0]['last_stage'], 'verify_callback')
        self.assertEqual(observed[0]['last_outcome'], 'processing')
        health = self.service.health()
        failure = health['request_diagnostics']['methods']['events/subscribe']['last_failure']
        self.assertEqual((failure['code'], failure['reason'], failure['stage']), (-32603, 'callback_failed', 'verify_callback'))
        self.assertNotIn(sensitive, json.dumps(health))
        self.assertNotIn(self.params['delivery']['url'], json.dumps(health))
        self.assertNotIn(self.secret, json.dumps(health))
        self.assertFalse(self.service.path.exists())

    def test_request_failure_diagnostics_distinguish_validation_routes_keys_and_storage(self):
        self.params['delivery']['secret'] = 'not-a-key'
        self.rpc('events/subscribe', self.params)
        slot = self.service.health()['request_diagnostics']['methods']['events/subscribe']
        self.assertEqual((slot['last_failure']['reason'], slot['last_failure']['stage']), ('invalid_signing_secret', 'validate'))
        self.params['delivery']['secret'] = self.secret
        with patch.object(self.engine, 'inbox_routes', side_effect=phase.Fault('NOT_FOUND')):
            self.rpc('events/subscribe', self.params)
        failure = self.service.health()['request_diagnostics']['methods']['events/subscribe']['last_failure']
        self.assertEqual((failure['reason'], failure['stage']), ('owner_or_route_unavailable', 'resolve_routes'))
        with patch.object(self.service, 'save', side_effect=OSError('private storage path')):
            self.rpc('events/subscribe', self.params)
        failure = self.service.health()['request_diagnostics']['methods']['events/subscribe']['last_failure']
        self.assertEqual((failure['reason'], failure['stage']), ('storage_failed', 'reserve_key_reference'))
        with patch.object(self.keys, 'put', side_effect=RuntimeError('private keychain data')):
            self.rpc('events/subscribe', self.params)
        failure = self.service.health()['request_diagnostics']['methods']['events/subscribe']['last_failure']
        self.assertEqual((failure['reason'], failure['stage']), ('keychain_unavailable', 'store_signing_key'))
        self.assertNotIn('private', json.dumps(self.service.health()))

    def test_request_diagnostics_never_persist_and_reset_on_facade_restart(self):
        self.rpc('events/list')
        self.rpc('events/subscribe', {})
        self.assertFalse(self.service.path.exists())
        self.subscribe()
        saved = self.service.path.read_text()
        self.assertNotIn('request_diagnostics', saved)
        self.assertNotIn('last_failure', saved)
        self.service.close()
        self.service = events.EventService(self.engine, self.keys, self.callback, start=False)
        self.engine.events = self.service
        methods = self.service.health()['request_diagnostics']['methods']
        self.assertTrue(all(slot['received_count'] == slot['accepted_count'] == slot['failed_count'] == 0 for slot in methods.values()))
        self.assertTrue(all(slot['last_failure'] is None for slot in methods.values()))

    def test_request_diagnostics_bounded_and_normalize_untrusted_event_errors(self):
        sensitive = 'https://private.example/whsec_secret_raw_cursor_handle'
        for _ in range(4):
            self.rpc('events/unrecognized-' + sensitive, {})
        with patch.object(self.service, '_call', side_effect=events.EventError(-99999, sensitive)):
            self.rpc('events/subscribe', self.params)
        health = self.service.health()
        methods = health['request_diagnostics']['methods']
        self.assertEqual(set(methods), set(events.EVENT_METHODS))
        self.assertEqual(methods['events/subscribe']['last_failure']['code'], -32603)
        self.assertEqual(methods['events/subscribe']['last_failure']['reason'], 'event_operation_failed')
        self.assertNotIn(sensitive, json.dumps(health))
        health['request_diagnostics']['methods']['events/subscribe']['received_count'] = 999
        self.assertEqual(self.service.health()['request_diagnostics']['methods']['events/subscribe']['received_count'], 1)

    def test_invalid_native_envelopes_are_counted_without_side_effects(self):
        result = mcp_server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'events/subscribe',
            'params': ['private data']}, self.running.path, self.engine)
        self.assertEqual(result['error']['code'], -32600)
        self.assertIsNone(mcp_server.handle({'jsonrpc': '2.0', 'method': 'events/subscribe',
            'params': self.params}, self.running.path, self.engine))
        slot = self.service.health()['request_diagnostics']['methods']['events/subscribe']
        self.assertEqual((slot['received_count'], slot['failed_count'], slot['accepted_count']), (2, 2, 0))
        self.assertEqual(slot['last_failure']['reason'], 'invalid_request')
        self.assertFalse(self.service.path.exists())
        self.assertEqual(self.callback.calls, [])

    def test_native_request_meta_is_accepted_ignored_and_not_persisted(self):
        sensitive = 'private-progress-cursor-delivery-secret-DO-NOT-RETAIN'
        metadata = {'progressToken': sensitive, 'trace': {'callback': sensitive}, 'principal': 'another-owner'}
        self.assertIn('result', self.rpc('events/list', {'_meta': metadata}))
        self.params['_meta'] = metadata
        result = self.subscribe()
        params = copy.deepcopy(self.params)
        params['delivery'].pop('secret')
        self.assertEqual(self.rpc('events/unsubscribe', params)['result'], {})
        self.assertNotIn(sensitive, self.service.path.read_text())
        self.assertNotIn(sensitive, json.dumps(self.service.health()))
        self.assertNotIn(sensitive, repr(self.callback.calls))
        self.assertEqual(set(self.service.health()['request_diagnostics']['methods']), set(events.EVENT_METHODS))
        for method in events.EVENT_METHODS:
            bad = {'_meta': []} if method == 'events/list' else dict(params, _meta=[])
            self.assertEqual(self.rpc(method, bad)['error']['code'], -32602)
        self.assertEqual(self.rpc('events/list', {'unexpected': True, '_meta': {}})['error']['code'], -32602)
        self.assertEqual(self.rpc('events/subscribe', dict(self.params, unexpected=True))['error']['code'], -32602)


    def audit_records(self):
        records = []
        for path in (self.service.audit.previous, self.service.audit.path):
            if path.exists():
                records.extend(json.loads(line) for line in path.read_text().splitlines())
        return records

    def test_audit_records_incoming_methods_and_whitelisted_tools_only(self):
        sensitive = 'private-url-whsec-keyref-message-id-cursor-DO-NOT-LOG'
        self.rpc('initialize', {'private': sensitive})
        self.rpc('tools/call', {'name': 'bridge_status', 'arguments': {'private': sensitive}})
        self.rpc('tools/call', {'name': sensitive, 'arguments': {'private': sensitive}})
        self.rpc(sensitive, {'private': sensitive})
        self.rpc('events/subscribe', {'_meta': {'private': sensitive}})
        records = self.audit_records()
        arrivals = [record for record in records if record['type'] == 'mcp_request']
        self.assertEqual([record['method'] for record in arrivals], ['initialize', 'tools/call', 'tools/call', 'other', 'events/subscribe'])
        self.assertEqual([record.get('tool') for record in arrivals], [None, 'bridge_status', 'other', None, None])
        self.assertTrue(all(record['at'].endswith('Z') for record in records))
        self.assertNotIn(sensitive, json.dumps(records))
        self.assertEqual(self.service.audit.path.stat().st_mode & 0o777, 0o600)
        outcome = next(record for record in records if record['type'] == 'event_request_outcome')
        self.assertEqual((outcome['stage'], outcome['code'], outcome['reason']), ('validate', -32602, 'invalid_event'))
        self.assertEqual(set(outcome), {'at', 'type', 'method', 'stage', 'outcome', 'code', 'reason'})

    def test_audit_standard_method_names_and_handler_errors_without_implementing_them(self):
        for method in ('server/discover', 'resources/list', 'prompts/list'):
            self.assertEqual(self.rpc(method)['error']['code'], -32601)
        self.assertIsNone(mcp_server.handle({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
            'params': {'requestId': 'private-handle'}}, self.running.path, self.engine))
        records = self.audit_records()
        arrivals = [record for record in records if record['type'] == 'mcp_request']
        self.assertEqual([record['method'] for record in arrivals],
            ['server/discover', 'resources/list', 'prompts/list', 'notifications/cancelled'])
        responses = [record for record in records if record['type'] == 'mcp_response']
        self.assertEqual([record['method'] for record in responses], ['server/discover', 'resources/list', 'prompts/list'])
        self.assertTrue(all(record['outcome'] == 'error' and record['error_code'] == -32601 for record in responses))
        self.assertNotIn('private-handle', json.dumps(records))

    def test_audit_initialization_versions_are_normalized_and_no_request_id_is_retained(self):
        sensitive = 'private-request-id-url-key-cursor-protocol'
        for asked, expected in (('2024-11-05', '2024-11-05'), ('2026-07-28', '2026-07-28'),
                                (sensitive, 'other'), (None, 'absent')):
            message = {'jsonrpc': '2.0', 'id': sensitive, 'method': 'initialize', 'params': {'protocolVersion': asked}}
            response = mcp_server.handle(message, self.running.path, self.engine)
            self.assertEqual(response['result']['protocolVersion'], '2025-11-25')
            request, result = self.audit_records()[-2:]
            self.assertEqual(request['requested_protocol'], expected)
            self.assertEqual(result['negotiated_protocol'], '2025-11-25')
            self.assertEqual((result['type'], result['outcome'], result['error_code']), ('mcp_response', 'result', None))
        self.assertNotIn(sensitive, json.dumps(self.audit_records()))

    def test_audit_response_excludes_error_text_and_preserves_known_error_code(self):
        sensitive = 'https://private.example/whsec_key_message_handle'
        with patch.object(self.service, '_call', side_effect=events.EventError(-32015, sensitive)):
            self.assertEqual(self.rpc('events/subscribe', self.params)['error']['code'], -32015)
        response = self.audit_records()[-1]
        self.assertEqual(response['type'], 'mcp_response')
        self.assertEqual(response['method'], 'events/subscribe')
        self.assertEqual((response['outcome'], response['error_code']), ('error', -32015))
        self.assertEqual(set(response), {'at', 'type', 'method', 'outcome', 'error_code'})
        self.assertNotIn(sensitive, json.dumps(self.audit_records()))

    def test_audit_persists_receipts_but_not_volatile_counters_across_restart(self):
        self.rpc('events/list')
        before = self.service.audit.path.read_bytes()
        self.service.close()
        self.service = events.EventService(self.engine, self.keys, self.callback, start=False)
        self.engine.events = self.service
        self.assertEqual(self.service.audit.path.read_bytes(), before)
        methods = self.service.health()['request_diagnostics']['methods']
        self.assertEqual(methods['events/list']['received_count'], 0)
        self.assertNotIn('received_count', before.decode())

    def test_audit_failure_is_visible_and_preserves_existing_request_behavior(self):
        sensitive = 'private storage exception with callbackURL and key'
        with patch.object(self.service.audit, 'append', side_effect=OSError(sensitive)):
            result = self.rpc('events/list')
        self.assertIn('result', result)
        health = self.service.health()
        self.assertEqual(health['audit_write_failures'], 3)
        self.assertEqual(health['audit_last_error'], 'audit_write_failed')
        self.assertEqual(health['request_diagnostics']['methods']['events/list']['accepted_count'], 1)
        self.assertNotIn(sensitive, json.dumps(health))
        self.assertFalse(self.service.audit.path.exists())

    def test_audit_rotation_is_bounded_private_and_thread_safe(self):
        root = Path(self.root) / 'isolated-audit'
        root.mkdir(mode=0o700)
        audit = events.ArrivalAudit(root, max_bytes=512)
        threads = [threading.Thread(target=lambda: [audit.write('mcp_request', method='tools/call', tool='bridge_status') for _ in range(10)]) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(audit.health()['audit_write_failures'], 0)
        self.assertEqual(set(path.name for path in root.iterdir()), {'events-audit.jsonl', 'events-audit.jsonl.1'})
        for path in (audit.path, audit.previous):
            self.assertLessEqual(path.stat().st_size, 512)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(all(json.loads(line)['method'] == 'tools/call' for line in path.read_text().splitlines()))

    def test_audit_refuses_symlinks_without_touching_target(self):
        target = Path(self.root) / 'do-not-touch'
        target.write_text('original')
        self.service.audit.path.symlink_to(target)
        result = self.rpc('events/list')
        self.assertIn('result', result)
        self.assertEqual(target.read_text(), 'original')
        self.assertEqual(self.service.health()['audit_write_failures'], 3)

    def test_audit_fifo_without_reader_fails_promptly_and_keeps_request_working(self):
        os.mkfifo(self.service.audit.path, 0o600)
        results = []
        worker = threading.Thread(target=lambda: results.append(self.rpc('events/list')), daemon=True)
        worker.start()
        worker.join(timeout=1)
        blocked = worker.is_alive()
        if blocked:
            # Release the planted FIFO before failing, so the negative control
            # cannot strand a blocked test thread or hang the suite.
            reader = os.open(self.service.audit.path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                worker.join(timeout=2)
            finally:
                os.close(reader)
        self.assertFalse(blocked, 'Audit blocked on a FIFO without a reader')
        self.assertFalse(worker.is_alive())
        self.assertIn('result', results[0])
        self.assertEqual(self.service.health()['audit_write_failures'], 3)

    def test_existing_watch_audits_independent_and_correlated_messages_without_bodies(self):
        self.service.close()
        self.service = events.EventService(self.engine, self.keys, self.callback, start=True)
        self.engine.events = self.service
        self.subscribe()
        self.send()
        session = self.owned[0]
        parent = self.running.call('send', project='test-project', sender=session['sender'],
            recipient=session['route'], body='private parent body', key='audit-parent')
        self.running.call('reply', id=parent['id'], project='test-project', body='private reply body', key='audit-reply')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            observed = [record for record in self.audit_records() if record['type'] == 'broker_message_observed']
            if {record['correlated'] for record in observed} == {False, True} and self.applications():
                break
            time.sleep(.02)
        self.assertEqual({record['correlated'] for record in observed}, {False, True})
        self.assertTrue(all(set(record) == {'at', 'type', 'correlated'} for record in observed))
        raw = json.dumps(self.audit_records())
        self.assertNotIn('private parent body', raw)
        self.assertNotIn('private reply body', raw)
        self.assertNotIn(parent['id'], raw)
        self.assertNotIn(self.params['delivery']['url'], raw)
        self.assertNotIn(self.secret, raw)
        self.assertEqual(len(self.applications()), 1)



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
