"""Issue #5 bars. All subprocesses use disposable sockets and routing state."""
import asyncio
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
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
import service
from support import RunningBroker
from test_codex_live import FakeDesktop


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

    def test_reverification_uses_exact_session_cwd_and_live_codex_owner(self):
        sid = str(uuid.uuid4())
        route = dict(project='demo', name='alpha', cwd=str(self.root), session=sid,
                     transport='codex', executable=None)
        target = dict(id=sid, cwd=str(self.root), runtime_observed=True, status='idle')
        with patch.object(route_store.sessions, 'inspect_session', return_value={'session': target}) as discover, \
                patch.object(route_store.codex_live, 'Client') as client:
            route_store.verify_route(route)
            discover.assert_called_once_with('codex', sid, None, codex_mode='auto')
            client.return_value.__enter__.return_value.owner.assert_called_once_with(sid)
        for bad in (dict(target, id=str(uuid.uuid4())), dict(target, cwd='/tmp')):
            with patch.object(route_store.sessions, 'inspect_session', return_value={'session': bad}), \
                    patch.object(route_store.codex_live, 'Client') as client:
                with self.assertRaisesRegex(ValueError, 'Session or workspace differs'):
                    route_store.verify_route(route)
                client.assert_not_called()

    def test_saved_codex_unknown_runtime_restores_when_exact_desktop_owner_is_live(self):
        sid = str(uuid.uuid4())
        self.register(transport='codex', session=sid)
        self.bind()
        before = deepcopy((self.state.endpoints, self.state.channels))
        saved = dict(id=sid, cwd=str(self.root), runtime_observed=False,
                     status='unknown', reported_status='notLoaded', scope='saved-metadata')
        with FakeDesktop() as desktop, \
                patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC': desktop.path}), \
                patch.object(route_store.sessions, 'inspect_session', return_value={'session': saved}) as discover:
            loaded = broker.State(store=self.store)
            discover.assert_called_once_with('codex', sid, None, codex_mode='auto')
            owners = [call for call in desktop.calls if call['method'] == 'thread-owner-discovery']
            self.assertEqual([call['params']['conversationId'] for call in owners], [sid])
            self.assertEqual({call['method'] for call in desktop.calls}, {'initialize', 'thread-owner-discovery'})
        self.assertEqual((loaded.endpoints, loaded.channels), before)

    def test_saved_codex_metadata_drops_without_capable_exact_desktop_owner(self):
        sid = str(uuid.uuid4())
        self.register(transport='codex', session=sid)
        self.bind()
        saved = dict(id=sid, cwd=str(self.root), runtime_observed=False, status='unknown')
        for behavior in ('no-owner', 'no-capability'):
            with self.subTest(behavior=behavior), FakeDesktop(behavior) as desktop, \
                    patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC': desktop.path}), \
                    patch.object(route_store.sessions, 'inspect_session', return_value={'session': saved}), \
                    redirect_stderr(io.StringIO()) as diagnostic:
                # Restore the frozen routing inputs before each independent load.
                self.store.save(self.state.endpoints, self.state.channels)
                loaded = broker.State(store=self.store)
                self.assertEqual((loaded.endpoints, loaded.channels), ({}, {}))
                self.assertEqual(diagnostic.getvalue().count('routing_drop'), 2)
                self.assertNotIn(sid, self.store.path.read_text())
                self.assertEqual({call['method'] for call in desktop.calls}, {'initialize', 'thread-owner-discovery'})

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

    def test_same_socket_restart_retains_routes_channels_but_no_messages(self):
        with RunningBroker() as server:
            server.register_pair()
            before = server.call('routes', project='demo'), server.call('channels', project='demo')
            message = server.call('send', project='demo', sender='alpha', recipient='beta', body='PRIVATE_BODY', key='key')
            instance = server.instance
            server.restart()
            self.assertNotEqual(instance, server.instance)
            self.assertEqual(before, (server.call('routes', project='demo'), server.call('channels', project='demo')))
            self.assertEqual(server.call('ping')['messages'], 0)
            with self.assertRaisesRegex(ValueError, 'Unknown message'):
                server.call('show', id=message['id'])
            raw = (Path(server.state_dir) / 'routes.json').read_text()
            self.assertNotIn('PRIVATE_BODY', raw)

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
