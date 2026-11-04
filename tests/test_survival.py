"""Issue #5 bars. All subprocesses use disposable sockets and routing state."""
import asyncio
from contextlib import redirect_stderr
from copy import deepcopy
import io
import errno
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
import broker
import route_store
import message_store
import service
from support import RunningBroker
from test_codex_live import FakeDesktop


class DurabilityTests(unittest.TestCase):
    def test_message_and_routing_writes_sync_file_then_replace_then_directory(self):
        with tempfile.TemporaryDirectory(prefix='ab94-sync-', dir='/tmp') as root:
            store = route_store.RouteStore(Path(root) / 'state')
            self.addCleanup(store.close)
            for write in (lambda: store.save({}, {}),
                          lambda: message_store.atomic_write(store.directory / 'mail.json', b'{}')):
                calls = []
                def record_sync(fd):
                    kind = 'directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file'
                    calls.append(('fsync', kind))
                def record_full_sync(fd, command):
                    self.assertEqual(command, route_store.fcntl.F_FULLFSYNC)
                    kind = 'directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file'
                    calls.append(('fullfsync', kind))
                replace = os.replace
                def record_replace(source, destination):
                    calls.append(('replace', 'file'))
                    replace(source, destination)
                with patch.object(route_store.os, 'fsync', side_effect=record_sync), \
                        patch.object(route_store.fcntl, 'F_FULLFSYNC', 51, create=True), \
                        patch.object(route_store.fcntl, 'fcntl', side_effect=record_full_sync), \
                        patch.object(route_store.os, 'replace', side_effect=record_replace):
                    write()
                self.assertEqual(calls, [('fsync', 'file'), ('fullfsync', 'file'),
                                         ('replace', 'file'), ('fsync', 'directory'),
                                         ('fullfsync', 'directory')])

    def test_unsupported_full_sync_falls_back_but_io_errors_propagate(self):
        for failure in (errno.EINVAL, errno.ENOTSUP, errno.ENOTTY, errno.ENOSYS):
            with self.subTest(errno=failure), \
                    patch.object(route_store.os, 'fsync') as sync, \
                    patch.object(route_store.fcntl, 'F_FULLFSYNC', 51, create=True), \
                    patch.object(route_store.fcntl, 'fcntl', side_effect=OSError(failure, 'unsupported')):
                route_store.sync_descriptor(123)
                sync.assert_called_once_with(123)
        with patch.object(route_store.os, 'fsync'), \
                patch.object(route_store.fcntl, 'F_FULLFSYNC', 51, create=True), \
                patch.object(route_store.fcntl, 'fcntl', side_effect=OSError(errno.EIO, 'I/O failure')):
            with self.assertRaises(OSError):
                route_store.sync_descriptor(123)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.state = broker.State(3)
        for name in ('alpha', 'beta'):
            self.call('register', project='demo', name=name, cwd='/tmp')

    def call(self, op, **fields):
        return self.state.action(dict(op=op, **fields))

    def send(self, key):
        return self.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key=key)

    def complete(self, message):
        self.call('resolve', id=message['id'], state='done', note='Explicitly completed')

    def test_oldest_terminal_evicted_and_auxiliary_maps_bounded(self):
        first = self.send('0')
        self.complete(first)
        for i in range(1, 20):
            message = self.send(str(i))
            self.complete(message)
        self.assertEqual(list(self.state.messages), [self.send(str(i))['id'] for i in range(17, 20)])
        self.assertEqual(len(self.state.answers), 3)
        self.assertEqual(len(self.state.keys), 3)
        with self.assertRaisesRegex(ValueError, 'Unknown message'):
            self.call('show', id=first['id'])
        # Explicit reuse after eviction is a new message, never an automatic retry.
        self.assertNotEqual(self.send('0')['id'], first['id'])

    def test_only_actual_terminal_states_are_candidates(self):
        for state in ('pending', 'submitted', 'received', 'in_progress', 'dispatching', 'uncertain',
                      'delivered', 'replied', 'expired'):
            with self.subTest(state=state):
                self.state.messages.clear(); self.state.keys.clear(); self.state.answers.clear()
                self.state.max_messages = 1
                message = self.send(state)
                self.state.status(message, state)
                with self.assertRaisesRegex(ValueError, 'no evictable terminal messages'):
                    self.send('next')
                self.assertEqual(self.call('show', id=message['id'])['state'], state)
        for state in ('done', 'answered', 'failed'):
            with self.subTest(terminal=state):
                self.state.messages.clear(); self.state.keys.clear(); self.state.answers.clear()
                message = self.send(state)
                self.state.status(message, state)
                self.send('next')
                self.assertNotIn(message['id'], self.state.messages)

    def test_invalid_send_or_reply_does_not_evict(self):
        self.state.max_messages = 1
        first = self.send('first'); self.complete(first)
        before = deepcopy(self.state.messages)
        for op, fields in (
            ('send', dict(project='demo', sender='alpha', recipient='missing', body='Q', key='bad')),
            ('send', dict(project='demo', sender='alpha', recipient='alpha', body='Q', key='self')),
            ('send', dict(project='demo', sender='alpha', recipient='beta', body='', key='blank')),
            ('reply', dict(id='missing', body='A', key='reply'))):
            with self.assertRaises(ValueError):
                self.call(op, **fields)
            self.assertEqual(self.state.messages, before)
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.call('send', project='demo', sender='alpha', recipient='beta', body='other', key='first')
        self.assertEqual(self.state.messages, before)

    def test_reply_parent_protected_before_marking_answered(self):
        self.state.max_messages = 2
        parent = self.send('parent'); self.complete(parent)
        other = self.send('other'); self.complete(other)
        answer = self.call('reply', id=parent['id'], body='A', key='reply')
        self.assertEqual(self.call('show', id=parent['id'])['state'], 'answered')
        self.assertTrue(self.state.answers[parent['id']].is_set())
        self.assertEqual(answer['reply_to'], parent['id'])
        self.assertNotIn(other['id'], self.state.messages)
        self.assertEqual(self.call('reply', id=parent['id'], body='A', key='reply')['id'], answer['id'])

    def test_full_pending_refuses_reply_without_mutating_parent(self):
        self.state.max_messages = 1
        parent = self.send('parent')
        with self.assertRaisesRegex(ValueError, 'no evictable terminal'):
            self.call('reply', id=parent['id'], body='A', key='reply')
        self.assertEqual(parent['state'], 'pending')
        self.assertFalse(self.state.answers[parent['id']].is_set())

    def test_active_wait_pins_parent_and_reply_until_consumed(self):
        async def check():
            self.state.max_messages = 2
            parent = self.send('parent')
            reader = asyncio.StreamReader()
            waiting = asyncio.create_task(broker.wait_answer(self.state, dict(id=parent['id'], timeout=1), reader))
            await asyncio.sleep(0)
            answer = self.call('reply', id=parent['id'], body='A', key='reply')
            self.complete(answer)
            with self.assertRaisesRegex(ValueError, 'protected'):
                self.send('pressure')
            result = await waiting
            self.assertEqual(result['replies'][0]['body'], 'A')
            self.assertEqual(self.state.waiters, {})
            self.send('pressure')
            self.assertNotIn(parent['id'], self.state.answers)
        asyncio.run(check())

    def test_timeout_does_not_make_pending_evictable(self):
        async def check():
            self.state.max_messages = 1
            parent = self.send('parent')
            result = await broker.wait_answer(self.state, dict(id=parent['id'], timeout=.01), asyncio.StreamReader())
            self.assertTrue(result['timed_out'])
            self.assertEqual(self.state.waiters, {})
            with self.assertRaisesRegex(ValueError, 'no evictable terminal'):
                self.send('next')
        asyncio.run(check())

    def test_wait_disconnect_and_cancellation_release_protection(self):
        async def check():
            for cancel in (False, True):
                parent = self.send('cancel' if cancel else 'disconnect')
                reader = asyncio.StreamReader()
                task = asyncio.create_task(broker.wait_answer(self.state, dict(id=parent['id'], timeout=1), reader))
                await asyncio.sleep(0)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    reader.feed_eof()
                    self.assertIsNone(await task)
                self.assertEqual(self.state.waiters, {})
        asyncio.run(check())


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab5-store-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = route_store.RouteStore(self.root / 'state')
        self.addCleanup(self.store.close)
        self.state = broker.State(store=self.store)
        self.register()

    def register(self, **extra):
        return self.state.action(dict(op='register', project='demo', name='alpha', cwd=str(self.root), **extra))

    def bind(self):
        return self.state.action(dict(op='channel-bind', project='demo', name='work', owner='alpha', description='Work'))

    def test_snapshot_is_private_and_allowlisted_no_messages_or_credentials(self):
        self.bind()
        self.state.endpoints['demo', 'alpha']['peerToken'] = 'SECRET_TOKEN'
        self.state.endpoints['demo', 'alpha']['socket'] = '/private/peer.sock'
        self.state.endpoints['demo', 'alpha']['pid'] = 42
        self.store.save(self.state.endpoints, self.state.channels)
        raw = self.store.path.read_text()
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        for secret in ('SECRET_TOKEN', '/private/peer.sock', 'peerToken', '"pid"', 'messages', 'answers', 'keys'):
            self.assertNotIn(secret, raw)
        loaded = broker.State(store=self.store)
        self.assertEqual(loaded.messages, {})
        self.assertEqual(loaded.channels, self.state.channels)

    def test_message_checkpoint_is_allowlisted_and_private(self):
        self.state.action(dict(op='register', project='demo', name='beta', cwd=str(self.root)))
        self.state.endpoints['demo', 'alpha'].update(peerToken='SECRET_TOKEN', socket='/private/peer.sock')
        message = self.state.action(dict(op='send', project='demo', sender='alpha', recipient='beta', body='body', key='key'))
        self.state.action(dict(op='claim', id=message['id']))
        self.state.action(dict(op='finish', id=message['id'], state='submitted', note='SECRET_DIAGNOSTIC'))
        path = self.root / 'state' / 'correspondence.json'
        raw = path.read_text()
        for forbidden in ('SECRET_TOKEN', 'SECRET_DIAGNOSTIC', '/private/peer.sock', 'peerToken'):
            self.assertNotIn(forbidden, raw)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        loaded = broker.State(store=self.store)
        self.assertEqual(loaded.get(message['id'])['state'], 'submitted')
        self.assertEqual(loaded.get(message['id'])['body'], 'body')

    def test_message_save_failure_does_not_publish_or_mutate(self):
        self.state.action(dict(op='register', project='demo', name='beta', cwd=str(self.root)))
        message = self.state.action(dict(op='send', project='demo', sender='alpha', recipient='beta', body='body', key='key'))
        before = deepcopy((self.state.messages, self.state.keys, list(self.state.events)))
        checkpoint = self.state.correspondence.path.read_bytes()
        for operation in (dict(op='claim', id=message['id']),
                          dict(op='reply', id=message['id'], body='answer', key='answer')):
            for failure in ('replace', 'fsync'):
                with self.subTest(op=operation['op'], failure=failure), patch.object(message_store.os, failure, side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError):
                        self.state.action(operation)
                self.assertEqual((self.state.messages, self.state.keys, list(self.state.events)), before)
                self.assertFalse(self.state.answers[message['id']].is_set())
                self.assertEqual(self.state.correspondence.path.read_bytes(), checkpoint)
                self.assertEqual(list(self.store.directory.glob('.correspondence-*')), [])

    def test_inbox_receipt_and_snapshot_survive_restart_without_sliding_expiry(self):
        self.state.action(dict(op='register', project='demo', name='beta', cwd=str(self.root)))
        messages = [self.state.action(dict(op='send', project='demo', sender='alpha', recipient='beta', body=key, key=key))
                    for key in ('first', 'second')]
        args = dict(owner='a' * 64, context='b' * 64,
                    routes=[dict(slot='slot', route=self.state.route('demo', name='beta'))])
        page = self.state.action(dict(args, op='inbox_list', limit=1))
        receipt = self.state.action(dict(args, op='inbox_ack', id=messages[0]['id']))
        for _ in range(2):
            loaded = broker.State(store=self.store)
            self.assertEqual(loaded.action(dict(args, op='inbox_ack', id=messages[0]['id'])), receipt)
            self.assertEqual(loaded.inbox_snapshots[page['snapshot']]['expires'], page['expires'])
            self.assertEqual(loaded.get(messages[0]['id'])['state'], 'done')
            self.assertEqual(loaded.get(messages[1]['id'])['state'], 'pending')

    def test_malformed_restored_fields_recover_before_startup_resave(self):
        self.state.action(dict(op='register', project='demo', name='beta', cwd=str(self.root)))
        messages = [self.state.action(dict(op='send', project='demo', sender='alpha', recipient='beta', body=key, key=key))
                    for key in ('first', 'second')]
        args = dict(owner='a' * 64, context='b' * 64,
                    routes=[dict(slot='slot', route=self.state.route('demo', name='beta'))])
        self.state.action(dict(args, op='inbox_list', limit=1))
        self.state.action(dict(args, op='inbox_ack', id=messages[0]['id']))
        valid = json.loads(self.state.correspondence.path.read_text())
        manifest = self.state.correspondence.manifest.read_bytes()
        changes = []
        for expiry in ({}, [], 'invalid', float('nan'), float('inf')):
            receipt = deepcopy(valid)
            receipt['receipts'][0][1]['expires'] = expiry
            receipt['receipts'][0][1]['result']['reconcile_until'] = expiry
            changes.append(receipt)
            snapshot = deepcopy(valid)
            next(iter(snapshot['snapshots'].values()))['expires'] = expiry
            changes.append(snapshot)
        intent = deepcopy(valid)
        intent['keys'][0][1]['body'] = {'nested': 'invalid'}
        changes.append(intent)
        binding = deepcopy(valid)
        next(iter(binding['snapshots'].values()))['owner'] = {'nested': 'invalid'}
        changes.append(binding)
        for index, raw in enumerate(changes):
            with self.subTest(index=index):
                self.state.correspondence.manifest.write_bytes(manifest)
                self.state.correspondence.path.write_text(json.dumps(raw))
                with redirect_stderr(io.StringIO()) as diagnostic:
                    restored = broker.State(store=self.store)
                self.assertEqual(restored.messages, {})
                self.assertIn('correspondence.json', diagnostic.getvalue())
                self.assertIn(messages[0]['id'], diagnostic.getvalue())
                self.assertTrue((self.store.directory / 'correspondence-lost.json').exists())

    def test_ack_checkpoint_failure_leaves_mail_unconsumed(self):
        self.state.action(dict(op='register', project='demo', name='beta', cwd=str(self.root)))
        message = self.state.action(dict(op='send', project='demo', sender='alpha', recipient='beta', body='first', key='first'))
        args = dict(op='inbox_ack', id=message['id'], owner='a' * 64, context='b' * 64,
                    routes=[dict(slot='slot', route=self.state.route('demo', name='beta'))])
        original_replace = message_store.os.replace
        def fail_checkpoint(source, destination):
            if Path(destination) == self.state.correspondence.path:
                raise OSError('checkpoint commit failure')
            return original_replace(source, destination)
        with patch.object(message_store.os, 'replace', side_effect=fail_checkpoint):
            with self.assertRaisesRegex(OSError, 'checkpoint commit failure'):
                self.state.action(args)
        self.assertEqual(self.state.get(message['id'])['state'], 'pending')
        self.assertEqual(self.state.inbox_receipts, {})
        loaded = broker.State(store=self.store)
        self.assertEqual(loaded.get(message['id'])['state'], 'pending')
        self.assertEqual(loaded.inbox_receipts, {})
        receipt = self.state.action(args)
        self.assertEqual(self.state.action(args), receipt)

    def test_save_failure_rolls_back_register_bind_and_close(self):
        self.bind()
        before_maps = deepcopy((self.state.endpoints, self.state.channels))
        before_file = self.store.path.read_bytes()
        mutations = [dict(op='register', project='demo', name='alpha', cwd=str(self.root), session=str(uuid.uuid4())),
                     dict(op='channel-bind', project='demo', name='work', owner='alpha', description='Next', expect_generation=1),
                     dict(op='channel-close', project='demo', name='work', expect_generation=1)]
        for failure in ('replace', 'fsync'):
            for args in mutations:
                with self.subTest(failure=failure, op=args['op']), patch.object(route_store.os, failure, side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError):
                        self.state.action(args)
                self.assertEqual((self.state.endpoints, self.state.channels), before_maps)
                self.assertEqual(self.store.path.read_bytes(), before_file)
                self.assertEqual(list(self.store.directory.glob('.routes-*')), [])

    def test_channels_keep_pinned_route_and_generation_independent_of_endpoint(self):
        old = str(uuid.uuid4()); new = str(uuid.uuid4())
        self.register(transport='codex', session=old)
        channel = deepcopy(self.bind())
        self.register(transport='codex', session=new)
        seen = []
        with patch.object(route_store, 'verify_route', side_effect=lambda route: seen.append(route['session'])):
            loaded = broker.State(store=self.store)
        self.assertEqual(set(seen), {old, new})
        self.assertEqual(loaded.endpoints['demo', 'alpha']['session'], new)
        self.assertEqual(loaded.channels['demo', 'work'], channel)

    def test_dead_pinned_channel_dropped_even_when_new_endpoint_is_live(self):
        old = str(uuid.uuid4()); new = str(uuid.uuid4())
        self.register(transport='agy', session=old); self.bind()
        self.register(transport='agy', session=new)
        def verify(route):
            if route['session'] == old:
                raise ValueError('SECRET_TOKEN /private/peer.sock')
        output = io.StringIO()
        with patch.object(route_store, 'verify_route', side_effect=verify), redirect_stderr(output):
            loaded = broker.State(store=self.store)
        self.assertEqual(loaded.endpoints['demo', 'alpha']['session'], new)
        self.assertEqual(loaded.channels, {})
        self.assertIn('routing_drop', output.getvalue())
        self.assertNotIn('SECRET_TOKEN', output.getvalue())
        self.assertNotIn('/private/peer.sock', output.getvalue())
        self.assertNotIn(old, self.store.path.read_text())

    def test_live_old_channel_can_survive_dead_current_endpoint(self):
        old = str(uuid.uuid4()); new = str(uuid.uuid4())
        self.register(transport='agy', session=old)
        channel = deepcopy(self.bind())
        self.register(transport='agy', session=new)
        def verify(route):
            if route['session'] == new:
                raise ValueError('Unavailable')
        with patch.object(route_store, 'verify_route', side_effect=verify), redirect_stderr(io.StringIO()):
            loaded = broker.State(store=self.store)
        self.assertEqual(loaded.endpoints, {})
        self.assertEqual(loaded.channels['demo', 'work'], channel)

    def test_provider_timeout_drops_routes_without_logging_provider_output(self):
        self.register(transport='agy', session=str(uuid.uuid4()))
        self.bind()
        with patch.object(route_store, 'verify_route', side_effect=subprocess.TimeoutExpired('SECRET', 1)), \
                redirect_stderr(io.StringIO()) as output:
            loaded = broker.State(store=self.store)
        self.assertEqual((loaded.endpoints, loaded.channels), ({}, {}))
        self.assertNotIn('SECRET', output.getvalue())

    def test_missing_workspace_drops_route_and_bound_channel_and_closed_generation_survives(self):
        self.bind()
        self.state.action(dict(op='channel-close', project='demo', name='work', expect_generation=1))
        restored = broker.State(store=self.store)
        self.assertFalse(restored.channels['demo', 'work']['active'])
        self.assertEqual(restored.channels['demo', 'work']['generation'], 2)
        with patch.object(route_store, 'verify_route', side_effect=FileNotFoundError), redirect_stderr(io.StringIO()):
            restored = broker.State(store=self.store)
        self.assertEqual((restored.endpoints, restored.channels), ({}, {}))

    def test_corrupt_snapshot_refuses_start_without_overwrite(self):
        self.store.path.write_text('{broken')
        with self.assertRaisesRegex(ValueError, 'Invalid routing snapshot'):
            broker.State(store=self.store)
        self.assertEqual(self.store.path.read_text(), '{broken')

    def test_state_directory_is_exclusive_across_sockets(self):
        with self.assertRaisesRegex(ValueError, 'already in use'):
            route_store.RouteStore(self.store.directory)

    def test_snapshot_symlink_refused(self):
        victim = self.root / 'victim'; victim.write_text('untouched')
        self.store.path.unlink(); self.store.path.symlink_to(victim)
        with self.assertRaises(OSError):
            self.store.load()
        self.assertEqual(victim.read_text(), 'untouched')

    def test_codex_reverification_checks_metadata_identity_without_owner_liveness(self):
        sid = str(uuid.uuid4())
        route = dict(project='demo', name='alpha', cwd=str(self.root), session=sid,
                     transport='codex', executable=None)
        target = dict(id=sid, cwd=str(self.root), runtime_observed=True, status='idle')
        with patch.object(route_store.sessions, 'inspect_session', return_value={'session': target}) as discover, \
                patch.object(bridge.codex_live, 'Client', side_effect=AssertionError('No owner IPC')) as client:
            route_store.verify_route(route)
            discover.assert_called_once_with('codex', sid, None, codex_mode='auto')
            client.assert_not_called()
        for bad, reason in ((dict(target, id=str(uuid.uuid4())), 'session UUID differs'),
                            (dict(target, cwd=str(self.root / 'other')), 'session workspace differs')):
            with patch.object(route_store.sessions, 'inspect_session', return_value={'session': bad}), \
                    self.assertRaisesRegex(ValueError, reason):
                route_store.verify_route(route)

    def test_idle_unknown_and_limit_blocked_codex_routes_survive_without_owner_contact(self):
        sid = str(uuid.uuid4())
        self.register(transport='codex', session=sid)
        self.bind()
        before = deepcopy((self.state.endpoints, self.state.channels))
        snapshot = self.store.path.read_bytes()
        for status in ('idle', 'unknown', 'notLoaded'):
            target = dict(id=sid, cwd=str(self.root), status=status, runtime_observed=False,
                          ordinaryUsageAllowed=False)
            with self.subTest(status=status), \
                    patch.object(route_store.sessions, 'inspect_session', return_value={'session': target}) as discover, \
                    patch.object(bridge.codex_live, 'Client', side_effect=AssertionError('No owner IPC')) as client:
                loaded = broker.State(store=self.store)
            discover.assert_called_once_with('codex', sid, None, codex_mode='auto')
            client.assert_not_called()
            self.assertEqual((loaded.endpoints, loaded.channels), before)
            self.assertEqual(self.store.path.read_bytes(), snapshot)

    def test_codex_route_survives_when_desktop_has_no_owner_or_capability(self):
        sid = str(uuid.uuid4())
        self.register(transport='codex', session=sid)
        self.bind()
        before = deepcopy((self.state.endpoints, self.state.channels))
        target = dict(id=sid, cwd=str(self.root), status='unknown')
        for behavior in ('no-owner', 'no-capability'):
            with self.subTest(behavior=behavior), FakeDesktop(behavior) as desktop, \
                    patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC': desktop.path}), \
                    patch.object(route_store.sessions, 'inspect_session', return_value={'session': target}):
                loaded = broker.State(store=self.store)
                self.assertEqual((loaded.endpoints, loaded.channels), before)
                self.assertEqual(desktop.calls, [])

    def test_unavailable_metadata_retains_codex_identity_without_rewriting_snapshot(self):
        sid = str(uuid.uuid4())
        self.register(transport='codex', session=sid)
        self.bind()
        before = deepcopy((self.state.endpoints, self.state.channels))
        snapshot = self.store.path.read_bytes()
        for error in (route_store.sessions.DiscoveryError('SECRET rate limited'),
                      route_store.sessions.DiscoveryError('different or missing thread'),
                      ConnectionRefusedError('SECRET peer socket'),
                      subprocess.TimeoutExpired('SECRET', 1), KeyError('cwd'), TypeError('SECRET')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(route_store.sessions, 'inspect_session', side_effect=error) as discover, \
                    patch.object(bridge.codex_live, 'Client', side_effect=AssertionError('No owner IPC')), \
                    redirect_stderr(io.StringIO()) as diagnostic:
                loaded = broker.State(store=self.store)
            self.assertEqual((loaded.endpoints, loaded.channels), before)
            self.assertEqual(self.store.path.read_bytes(), snapshot)
            discover.assert_called_once_with('codex', sid, None, codex_mode='auto')
            self.assertIn('routing_retain', diagnostic.getvalue())
            self.assertIn('retained unverified', diagnostic.getvalue())
            self.assertNotIn('routing_drop', diagnostic.getvalue())
            self.assertNotIn('SECRET', diagnostic.getvalue())

    def test_incomplete_provider_metadata_is_not_proof_of_deletion(self):
        sid = str(uuid.uuid4())
        self.register(transport='codex', session=sid)
        self.bind()
        before = deepcopy((self.state.endpoints, self.state.channels))
        for observation in (None, {}, {'session': None}, {'session': {}},
                            {'session': {'id': sid}}, {'session': {'cwd': str(self.root)}},
                            {'session': {'id': '', 'cwd': str(self.root)}},
                            {'session': {'id': 'not-a-uuid', 'cwd': str(self.root)}}):
            with self.subTest(observation=observation), \
                    patch.object(route_store.sessions, 'inspect_session', return_value=observation), \
                    redirect_stderr(io.StringIO()) as diagnostic:
                loaded = broker.State(store=self.store)
            self.assertEqual((loaded.endpoints, loaded.channels), before)
            self.assertIn('metadata incomplete', diagnostic.getvalue())
            self.assertNotIn('routing_drop', diagnostic.getvalue())

    def test_invalid_or_missing_stored_codex_uuid_is_dropped_before_provider_access(self):
        sid = str(uuid.uuid4())
        self.register(transport='codex', session=sid)
        self.bind()
        valid = json.loads(self.store.path.read_text())
        for invalid in (None, [], 123, '', 'not-a-uuid', sid.upper(), 'missing-field'):
            raw = deepcopy(valid)
            for route in (raw['endpoints'][0], raw['channels'][0]['route']):
                if invalid == 'missing-field':
                    route.pop('session')
                else:
                    route['session'] = invalid
            self.store.path.write_text(json.dumps(raw))
            with self.subTest(invalid=invalid), \
                    patch.object(route_store.sessions, 'inspect_session') as discover, \
                    redirect_stderr(io.StringIO()) as diagnostic:
                loaded = broker.State(store=self.store)
            self.assertEqual((loaded.endpoints, loaded.channels), ({}, {}))
            discover.assert_not_called()
            reason = 'stored session field missing' if invalid == 'missing-field' else 'stored session UUID invalid'
            self.assertEqual(diagnostic.getvalue().count(reason), 2)

    def test_missing_codex_workspace_is_dropped_with_specific_safe_check(self):
        sid = str(uuid.uuid4())
        self.register(transport='codex', session=sid)
        self.bind()
        raw = json.loads(self.store.path.read_text())
        for route in (raw['endpoints'][0], raw['channels'][0]['route']):
            route['cwd'] = str(self.root / 'missing')
        self.store.path.write_text(json.dumps(raw))
        with patch.object(route_store.sessions, 'inspect_session') as discover, \
                redirect_stderr(io.StringIO()) as diagnostic:
            loaded = broker.State(store=self.store)
        self.assertEqual((loaded.endpoints, loaded.channels), ({}, {}))
        discover.assert_not_called()
        self.assertEqual(diagnostic.getvalue().count('workspace unavailable'), 2)
        self.assertNotIn(str(self.root), diagnostic.getvalue())

    def test_confirmed_codex_metadata_mismatch_drops_with_specific_check(self):
        sid = str(uuid.uuid4())
        self.register(transport='codex', session=sid)
        self.bind()
        for target, reason in ((dict(id=str(uuid.uuid4()), cwd=str(self.root)), 'session UUID differs'),
                               (dict(id=sid, cwd=str(self.root / 'other')), 'session workspace differs')):
            self.store.save(self.state.endpoints, self.state.channels)
            with self.subTest(reason=reason), \
                    patch.object(route_store.sessions, 'inspect_session', return_value={'session': target}), \
                    redirect_stderr(io.StringIO()) as diagnostic:
                loaded = broker.State(store=self.store)
            self.assertEqual((loaded.endpoints, loaded.channels), ({}, {}))
            self.assertEqual(diagnostic.getvalue().count(reason), 2)
            self.assertNotIn(sid, self.store.path.read_text())
            self.assertNotIn(str(self.root), diagnostic.getvalue())

    def test_unavailable_codex_endpoint_keeps_independent_older_pinned_channel(self):
        old, new = str(uuid.uuid4()), str(uuid.uuid4())
        self.register(transport='codex', session=old)
        channel = deepcopy(self.bind())
        self.register(transport='codex', session=new)
        def inspect(provider, sid, executable, codex_mode):
            if sid == new:
                raise route_store.sessions.DiscoveryError('temporarily unavailable')
            return {'session': {'id': old, 'cwd': str(self.root)}}
        with patch.object(route_store.sessions, 'inspect_session', side_effect=inspect) as discover, \
                redirect_stderr(io.StringIO()):
            loaded = broker.State(store=self.store)
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(loaded.endpoints['demo', 'alpha']['session'], new)
        self.assertEqual(loaded.channels['demo', 'work'], channel)
        self.assertEqual(loaded.channels['demo', 'work']['route']['session'], old)

    def test_peer_and_agy_restore_never_run_claude_cli(self):
        sid = str(uuid.uuid4())
        route = dict(project='demo', name='alpha', cwd=str(self.root), session=sid,
                     transport='claude-peer', executable=None)
        path = str(self.root / 'peer.sock')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.bind(path)
            target = dict(id=sid, cwd=str(self.root), socket=path)
            with patch.object(route_store.sessions, 'resolve_claude_peer', return_value=target) as resolve, \
                    patch.object(subprocess, 'Popen', side_effect=AssertionError('No CLI')):
                route_store.verify_route(route)
                resolve.assert_called_once_with(sid)
        route['transport'] = 'agy'
        with patch.object(route_store.sessions, 'inspect_session', return_value={'session': dict(id=sid, cwd=str(self.root))}) as inspect, \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('No CLI')):
            route_store.verify_route(route)
            inspect.assert_called_once_with('agy', sid, None, codex_mode='auto')


class SurvivalSocketTests(unittest.TestCase):
    def test_past_retention_limit_and_all_pending_refusal_over_real_socket(self):
        with RunningBroker(max_messages=3) as server:
            server.register_pair()
            for i in range(12):
                message = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key=str(i))
                server.call('resolve', id=message['id'], state='done', note='Completed by receiver')
            self.assertEqual(server.call('ping')['messages'], 3)
            for i in range(3):
                server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='pending-'+str(i))
            with self.assertRaisesRegex(ValueError, 'no evictable terminal messages'):
                server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='blocked')

    def test_same_socket_restart_retains_routes_channels_and_pending_messages(self):
        with RunningBroker() as server:
            server.register_pair()
            before = server.call('routes', project='demo'), server.call('channels', project='demo')
            message = server.call('send', project='demo', sender='alpha', recipient='beta', body='PRIVATE_BODY', key='key')
            instance = server.instance
            server.restart()
            self.assertNotEqual(instance, server.instance)
            self.assertEqual(before, (server.call('routes', project='demo'), server.call('channels', project='demo')))
            self.assertEqual(server.call('ping')['messages'], 1)
            self.assertEqual(server.call('show', id=message['id']), message)
            reply = server.call('reply', id=message['id'], body='answer', key='answer')
            self.assertEqual(reply['reply_to'], message['id'])
            self.assertEqual(reply['conversation'], message['conversation'])
            raw = (Path(server.state_dir) / 'routes.json').read_text()
            self.assertNotIn('PRIVATE_BODY', raw)

    def test_two_restarts_preserve_ids_keys_and_do_not_redeliver_submitted(self):
        with RunningBroker() as server:
            server.register_pair()
            counter = Path(server.temp.name) / 'deliveries'
            counter.write_text('0')
            message = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='once')
            script = ('from pathlib import Path; p = Path(' + repr(str(counter))
                      + '); p.write_text(str(int(p.read_text()) + 1)); print("accepted")')
            # Use the real deliver entry point with a disposable counting process.
            # This stub never invokes a provider or discovers a live session.
            with patch.object(bridge, 'command', return_value=[sys.executable, '-c', script]):
                bridge.deliver(server.path, message['id'])
            for _ in range(2):
                server.restart()
                self.assertEqual(server.call('show', id=message['id'])['state'], 'submitted')
                duplicate = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='once')
                self.assertEqual(duplicate['id'], message['id'])
                with patch.object(bridge, 'command', return_value=[sys.executable, '-c', script]):
                    with self.assertRaisesRegex(ValueError, 'Only pending'):
                        bridge.deliver(server.path, message['id'])
                self.assertEqual(server.call('ping')['messages'], 1)
            self.assertEqual(counter.read_text(), '1')

    def test_crash_during_dispatch_restores_uncertain_and_blocks_claim(self):
        with RunningBroker() as server:
            server.register_pair()
            message = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='once')
            server.call('claim', id=message['id'])
            server.process.kill()
            server.process.communicate(timeout=2)
            server.start()
            self.assertEqual(server.call('show', id=message['id'])['state'], 'uncertain')
            with self.assertRaisesRegex(ValueError, 'Only pending'):
                server.call('claim', id=message['id'])

    def test_terminal_messages_never_return_to_pending(self):
        with RunningBroker() as server:
            server.register_pair()
            message = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='done')
            server.call('resolve', id=message['id'], state='done', note='consumed')
            server.restart()
            self.assertEqual(server.call('show', id=message['id'])['state'], 'done')
            self.assertEqual(server.call('inbox', project='demo', name='beta'), [])

    def test_corrupt_and_truncated_checkpoint_report_durable_lost_identities(self):
        for content in ('{', '{"version":1,"messages":false}'):
            with self.subTest(content=content), RunningBroker() as server:
                server.register_pair()
                message = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='lost')
                server.call('shutdown')
                server.process.communicate(timeout=2)
                path = Path(server.state_dir) / 'correspondence.json'
                path.write_text(content)
                server.start()
                self.assertEqual(server.call('ping')['messages'], 0)
                report = json.loads(path.with_name('correspondence-lost.json').read_text())
                self.assertEqual(report['lost'][0]['id'], message['id'])
                self.assertEqual(report['lost'][0]['sender'], 'alpha')
                self.assertEqual(report['lost'][0]['recipient'], 'beta')
                self.assertEqual(report['lost'][0]['created'], message['created'])
                self.assertTrue(report['reason'])
                server.call('shutdown')
                _, diagnostic = server.process.communicate(timeout=2)
                self.assertIn('correspondence.json', diagnostic)
                self.assertIn(message['id'], diagnostic)

    def test_malformed_idempotency_rows_start_with_named_durable_loss(self):
        for rows in ([[]], [['key']], [None], [[[], {}, 'id']]):
            with self.subTest(rows=rows), RunningBroker() as server:
                server.register_pair()
                message = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='lost')
                server.call('shutdown')
                server.process.communicate(timeout=2)
                path = Path(server.state_dir) / 'correspondence.json'
                data = json.loads(path.read_text())
                data['keys'] = rows
                content = json.dumps(data)
                path.write_text(content)
                server.start()
                self.assertEqual(server.call('ping')['messages'], 0)
                report = json.loads(path.with_name('correspondence-lost.json').read_text())
                self.assertEqual(report['lost'][0]['id'], message['id'])
                self.assertEqual(path.with_name('correspondence-corrupt.json').read_text(), content)
                server.call('shutdown')
                _, diagnostic = server.process.communicate(timeout=2)
                self.assertIn('correspondence.json', diagnostic)
                self.assertIn(message['id'], diagnostic)

    def test_deeply_nested_checkpoint_starts_with_named_durable_loss(self):
        with RunningBroker() as server:
            server.register_pair()
            message = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='lost')
            server.call('shutdown')
            server.process.communicate(timeout=2)
            path = Path(server.state_dir) / 'correspondence.json'
            content = '[' * 20000 + '0' + ']' * 20000
            path.write_text(content)
            server.start()
            self.assertEqual(server.call('ping')['messages'], 0)
            report = json.loads(path.with_name('correspondence-lost.json').read_text())
            self.assertEqual(report['lost'][0]['id'], message['id'])
            self.assertEqual(path.with_name('correspondence-corrupt.json').read_text(), content)
            server.call('shutdown')
            _, diagnostic = server.process.communicate(timeout=2)
            self.assertIn('correspondence.json', diagnostic)
            self.assertIn(message['id'], diagnostic)

    def test_malformed_loss_ledgers_are_named_and_quarantined(self):
        contents = ('{', '{"previous":{}}', '{"previous":[null]}',
                    '[' * 20000 + '0' + ']' * 20000)
        for content in contents:
            for corrupt_checkpoint in (False, True):
                with self.subTest(content=content[:30], corrupt_checkpoint=corrupt_checkpoint), RunningBroker() as server:
                    server.register_pair()
                    message = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key='lost')
                    server.call('shutdown')
                    server.process.communicate(timeout=2)
                    directory = Path(server.state_dir)
                    ledger = directory / 'correspondence-lost.json'
                    ledger.write_text(content)
                    ledger.chmod(0o600)
                    if corrupt_checkpoint:
                        (directory / 'correspondence.json').write_text('{')
                    server.start()
                    self.assertTrue(server.call('ping')['correspondence_lost'])
                    self.assertEqual((directory / 'correspondence-lost-corrupt.json').read_text(), content)
                    report = json.loads(ledger.read_text())
                    if corrupt_checkpoint:
                        self.assertEqual(report['lost'][0]['id'], message['id'])
                        self.assertEqual(report['previous'][0]['file'], ledger.name)
                    else:
                        self.assertEqual(server.call('show', id=message['id'])['state'], 'pending')
                        self.assertEqual(report['file'], ledger.name)
                        self.assertTrue(report['unknown_identities'])
                    server.call('shutdown')
                    _, diagnostic = server.process.communicate(timeout=2)
                    self.assertIn(ledger.name, diagnostic)
                    self.assertIn('correspondence-lost-corrupt.json', diagnostic)

    def test_crash_restart_reloads_committed_routing_from_stale_socket(self):
        with RunningBroker() as server:
            server.register_pair()
            before = server.call('routes', project='demo'), server.call('channels', project='demo')
            server.process.kill()  # Only this disposable broker, never the default socket.
            server.process.communicate(timeout=2)
            self.assertTrue(Path(server.path).exists())
            server.start()
            self.assertEqual(before, (server.call('routes', project='demo'), server.call('channels', project='demo')))
            self.assertEqual(server.call('ping')['messages'], 0)

    def test_term_under_one_second_with_open_watch_wait_and_partial_request(self):
        with RunningBroker() as server:
            server.register_pair()
            for i in range(3):
                message = server.call('send', project='demo', sender='alpha', recipient='beta', body='Q', key=str(i))
            clients = []
            try:
                for payload in (dict(op='watch', project='demo', name='beta'),
                                dict(op='wait', id=message['id'], timeout=60), None):
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.settimeout(2); client.connect(server.path); clients.append(client)
                    client.sendall(broker.wire(payload) if payload else b'{')
                    if payload and payload['op'] == 'watch':
                        self.assertIn(b'subscribed', client.recv(4096))
                time.sleep(.03)
                started = time.monotonic()
                server.process.send_signal(signal.SIGTERM)
                stdout, stderr = server.process.communicate(timeout=1)
                elapsed = time.monotonic() - started
                print(json.dumps(dict(measurement='TERM active watch/wait/partial', seconds=elapsed,
                                      returncode=server.process.returncode)), flush=True)
                self.assertEqual(server.process.returncode, 0, stderr)
                self.assertLess(elapsed, 1)
                self.assertFalse(Path(server.path).exists())
                self.assertEqual(stderr, '')
                server.start()  # Lock and routing storage are reusable immediately.
                self.assertEqual(len(server.call('routes', project='demo')), 2)
            finally:
                for client in clients:
                    client.close()

    def test_existing_stop_subcommand_stops_watch_cleanly(self):
        with RunningBroker() as server:
            server.register_pair()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2); client.connect(server.path)
                client.sendall(broker.wire(dict(op='watch', project='demo', name='beta')))
                client.recv(4096)
                result = subprocess.run([sys.executable, bridge.__file__, '--socket', server.path, 'stop'],
                                        capture_output=True, text=True, timeout=2)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)['stopping'])
                server.process.communicate(timeout=1)
                self.assertEqual(server.process.returncode, 0)


class ServiceTests(unittest.TestCase):
    def test_generation_requires_review_path_outside_launch_agents(self):
        with tempfile.TemporaryDirectory(prefix='ab5-home-', dir='/tmp') as root, \
                patch.object(service.Path, 'home', return_value=Path(root)), \
                patch.object(service, 'launchctl', side_effect=AssertionError('No service calls')):
            with self.assertRaisesRegex(ValueError, 'explicit --plist'):
                service.install(root+'/b.sock', generate_only=True)
            with self.assertRaisesRegex(ValueError, 'outside LaunchAgents'):
                service.install(root+'/b.sock', plist=Path(root)/'Library/LaunchAgents/review.plist', generate_only=True)
            self.assertFalse((Path(root)/'Library').exists())

    def test_service_labels_differ_per_socket_and_failures_preserve_plist(self):
        with tempfile.TemporaryDirectory(prefix='ab5-service-', dir='/tmp') as root:
            first = service.configuration(root+'/a.sock', root+'/state', 3)
            second = service.configuration(root+'/b.sock', root+'/state', 3)
            self.assertNotEqual(first['Label'], second['Label'])
            path = Path(root)/'agent.plist'
            # Exercise the install/uninstall branches with launchctl mocked;
            # never bootstrap, load, or bootout an actual service in tests.
            with patch.object(service.sys, 'platform', 'darwin'), \
                    patch.object(service, 'launchctl', side_effect=ValueError('Unavailable')) as ctl:
                with self.assertRaisesRegex(ValueError, 'Unavailable'):
                    service.install(root+'/a.sock', root+'/state', path, max_messages=3)
                ctl.assert_called_once_with('bootstrap', f'gui/{os.getuid()}', str(path))
                self.assertTrue(path.exists())
                ctl.reset_mock()
                with self.assertRaisesRegex(ValueError, 'Unavailable'):
                    service.uninstall(root+'/a.sock', root+'/state', path)
                ctl.assert_called_once_with('bootout', f'gui/{os.getuid()}/{first["Label"]}')
                self.assertTrue(path.exists())

    def test_generation_and_removal_never_load_a_service(self):
        with tempfile.TemporaryDirectory(prefix='ab5-plist-', dir='/tmp') as root:
            path = Path(root) / 'agent.plist'
            socket_path = str(Path(root) / 'b.sock')
            with patch.object(service, 'launchctl', side_effect=AssertionError('No service calls')):
                result = service.install(socket_path, str(Path(root) / 'state'), path, generate_only=True, max_messages=5)
                self.assertFalse(result['installed'])
                data = plistlib.loads(path.read_bytes())
                self.assertIs(data['KeepAlive'], True)
                self.assertIs(data['RunAtLoad'], True)
                self.assertEqual(data['Umask'], 0o077)
                self.assertEqual(data['ProgramArguments'][-2:], ['--max-messages', '5'])
                self.assertIn(socket_path, data['ProgramArguments'])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    service.install(socket_path, plist=path, generate_only=True)
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    service.uninstall(socket_path+'-other', str(Path(root) / 'state'), path, generate_only=True)
                service.uninstall(socket_path, str(Path(root) / 'state'), path, generate_only=True)
                self.assertFalse(path.exists())

    def test_cli_generate_only_plist_is_valid_on_macos(self):
        with tempfile.TemporaryDirectory(prefix='ab5-plist-', dir='/tmp') as root:
            path = str(Path(root) / 'agent.plist')
            result = subprocess.run([sys.executable, bridge.__file__, '--socket', root+'/b.sock',
                                     'install-service', '--state-dir', root+'/state', '--plist', path, '--generate-only'],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['installed'])
            self.assertFalse(Path(root+'/state').exists())
            if sys.platform == 'darwin':
                lint = subprocess.run(['plutil', '-lint', path], capture_output=True, text=True, timeout=3)
                self.assertEqual(lint.returncode, 0, lint.stdout+lint.stderr)
            else:
                self.assertTrue(plistlib.loads(Path(path).read_bytes())['Label'].startswith(service.LABEL+'.'))


if __name__ == '__main__':
    unittest.main()
