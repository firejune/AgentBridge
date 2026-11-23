"""The Desktop queue's checks, run without accessing a live Desktop app or store."""
from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import codex_archive
import codex_live
import codex_preview
import codex_queue
import codex_wake_open
import delivery
from codex_live import ThreadArchived
from support import RunningBroker

UID = str(os.getuid())
HELPER = (str(codex_queue.APP) + '/Contents/Frameworks/Codex Framework.framework/Versions/158.0/'
          'Helpers/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)')
SERVER = f'{codex_queue.CLI} app-server --analytics-default-enabled'
CUA_SERVER = f'{codex_queue.CLI} app-server --listen stdio://'
OVERRIDES = ('CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_EXEC_SERVER_URL', 'AGENT_BRIDGE_CODEX_IPC')


def processes(pids='4242\n', server=None, parent=None):
    """A fake `read` for lsof and the two ps calls desktop_queue_owner makes."""
    def read(argv):
        if argv[0].endswith('lsof'):
            return pids
        if argv[3] == '4242':
            return server if server is not None else f'  {UID}  77 {SERVER}\n'
        return parent if parent is not None else f'  {UID} {HELPER}\n'
    return read


class QueueOwnerTests(unittest.TestCase):
    def test_one_bundled_app_server_under_the_desktop_helper_is_the_owner(self):
        self.assertEqual(codex_queue.desktop_queue_owner('queue', processes()), ('4242', '77', HELPER))
        # lsof repeats a pid once per open descriptor.
        self.assertEqual(codex_queue.desktop_queue_owner('queue', processes('4242\n4242\n')),
                         ('4242', '77', HELPER))

    def test_anything_else_holding_or_shaping_the_queue_is_refused(self):
        other = str(os.getuid() + 1)
        cases = {
            'two holders': processes('4242\n4243\n'),
            'no holder': processes(''),
            'another user': processes(server=f'{other} 77 {SERVER}\n'),
            'another binary': processes(server=f'{UID} 77 /usr/local/bin/codex app-server\n'),
            'not an app-server': processes(server=f'{UID} 77 {codex_queue.CLI} exec\n'),
            'listening server': processes(server=f'{UID} 77 {SERVER} --listen stdio://\n'),
            'listening server, joined flag': processes(server=f'{UID} 77 {SERVER} --listen=ws://x\n'),
            'parent of another user': processes(parent=f'{other} {HELPER}\n'),
            'parent outside the app': processes(parent=f'{UID} /tmp/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)\n'),
            'parent is not the helper': processes(parent=f'{UID} {codex_queue.APP}/Contents/Frameworks/Codex Framework.framework/Versions/1/Helpers/Other\n'),
        }
        for name, read in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(codex_queue.desktop_queue_owner('queue', read))

    def test_conflict_lists_unique_holders_with_verified_roles_and_elapsed_time(self):
        def read(argv):
            if argv[0].endswith('lsof'):
                return '4244\n4243\n4242\n4243\n'
            if argv[-1].endswith('command='):
                self.assertEqual(argv[3], '4242,4243,4244')
                wrapper_exec = codex_queue.CLI_BUNDLE.parent / 'bin/..' / codex_queue.CLI.relative_to(
                    codex_queue.CLI_BUNDLE.parent)
                return (f'4242 {UID} 77 02:03:04 {codex_queue.CLI} -c key=value app-server\n'
                        f'4243 {UID} 78 01:05 {wrapper_exec} app-server --listen stdio://\n'
                        f'4244 {UID} 79 01:06 {CUA_SERVER} PRIVATE_ARGUMENT\n')
            return (f'77 {UID} {HELPER}\n78 {UID} {codex_queue.NODE_REPL}\n'
                    f'79 {UID} /tmp/node_repl\n4242 {UID} {codex_queue.CLI}\n'
                    f'4243 {UID} {codex_queue.CLI}\n4244 {UID} /tmp/codex\n')
        with self.assertRaises(codex_queue.QueueOwnerUnavailable) as raised:
            codex_queue.desktop_queue_owner('queue', read, explain=True)
        detail = str(raised.exception)
        self.assertIn('pid 4242 (Desktop app-server, parent pid 77, elapsed 02:03:04)', detail)
        self.assertIn('pid 4243 (bundled node_repl app-server (CUA), parent pid 78, elapsed 01:05)', detail)
        self.assertIn('pid 4244 (unverified, parent pid 79, elapsed 01:06)', detail)
        self.assertEqual(detail.count('pid 4243'), 1)
        self.assertIn("Stop it only with the owner's approval.", detail)
        for private in (str(codex_queue.CLI), str(codex_queue.NODE_REPL), 'PRIVATE_ARGUMENT'):
            self.assertNotIn(private, detail)

    def test_refusal_retains_pids_when_processes_exit_and_handles_zero_holders(self):
        def disappeared(argv):
            if argv[0].endswith('lsof'):
                return '4242\n4243\n'
            raise subprocess.CalledProcessError(1, argv)
        with self.assertRaisesRegex(codex_queue.QueueOwnerUnavailable,
                                    r'pid 4243 \(unverified, parent pid unknown, elapsed unknown\)'):
            codex_queue.desktop_queue_owner('queue', disappeared, explain=True)
        with self.assertRaisesRegex(codex_queue.QueueOwnerUnavailable, 'No process holds the queue store'):
            codex_queue.desktop_queue_owner('queue', processes(''), explain=True)


class QueueNoticeTests(unittest.TestCase):
    def test_the_notice_is_a_pointer_that_defers_to_the_retrieved_message(self):
        message = {'id': str(uuid.uuid4()), 'sender': 'a', 'recipient': 'b', 'project': 'p', 'body': 'PEER BODY',
                   'reply_to': None, 'sender_channel': None, 'channel': None}
        notice = codex_queue.notice(message['id'])
        self.assertTrue(notice.endswith('bridge.py receive-codex ' + message['id']))
        self.assertNotIn(message['body'], notice)
        # The notice must not outrank the message: a woken thread read "notification only", and
        # later a scope sentence in the notice, as a limit to retrieval and left the request undone.
        self.assertIn('only a pointer', notice)
        self.assertIn('the retrieved message takes precedence over it', notice)
        self.assertIn('does not limit this turn to retrieval', notice)
        self.assertNotIn('notification only', notice)
        self.assertNotIn(delivery.PEER_SCOPE, notice)
        # It grants nothing itself: what may be done comes from the user's standing rules.
        self.assertIn("apply the user's standing delegation rules", notice)
        self.assertIn('provenance and authorization as retrieved', notice)
        self.assertNotIn('authorized by', notice)
        # The scope sentence still reaches the thread, in the envelope receive-codex returns.
        self.assertIn(delivery.PEER_SCOPE, delivery.envelope(message))


class CloseHandoffQueueTests(unittest.TestCase):
    def setUp(self):
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        self.profile = Path(profile.name).resolve()
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.profile), 'CODEX_SQLITE_HOME': str(self.profile)})
        env.start()
        self.addCleanup(env.stop)
        self.server = RunningBroker()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.operator, self.target = str(uuid.uuid4()), str(uuid.uuid4())
        self.cwd = str(Path(self.server.temp.name).resolve())
        db_path = self.profile / codex_preview.STATE_FILE
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute('CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, updated_at INTEGER, '
                       'archived INTEGER, archived_at INTEGER)')
            db.execute("INSERT INTO threads VALUES (?, 'fixture', ?, 1, 0, NULL)", (self.target, self.cwd))
            db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)')
        self.server.call('register', project='work', name='worker', cwd=self.cwd,
                         transport='codex', session=self.target)
        self.server.call('register', project='ops', name='operator', cwd=self.cwd,
                         transport='codex', session=self.operator)
        self.server.call('register', project='ops', name='requester', cwd=self.cwd, transport='realtime')
        wake = patch.object(codex_wake_open, 'request_queued_wake')
        self.wake = wake.start()
        self.addCleanup(wake.stop)
        config = Path(self.server.state_dir) / 'close-operator.json'
        config.write_text(json.dumps({'project': 'ops', 'name': 'operator', 'session': self.operator,
                                      'sender': 'requester', 'prefer_native_for_direct': True}), encoding='utf-8')
        config.chmod(0o600)
        endpoint = codex_live.EndpointUnavailable
        for patcher in (
            patch.object(bridge.broker, 'DEFAULT_SOCKET', Path(self.server.path)),
            patch.object(codex_archive, 'executable', return_value='/fixture/codex'),
            patch.object(codex_archive.wt, 'open_files', return_value=[]),
            patch.object(codex_preview, 'state_path', return_value=db_path),
            patch.object(codex_live, 'exact_saved_workspace', return_value=(None, None)),
            patch.object(codex_live.Client, 'connect', side_effect=endpoint(endpoint.DEFAULT, endpoint.MISSING)),
            patch.object(codex_queue, 'check_route'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cli(self, *args):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = bridge.main(list(args))
        return result, json.loads(stdout.getvalue())

    def handoff(self, *flags):
        return self.cli('--socket', self.server.path, 'close', 'worker', '--project', 'work',
                        '--archive', '--state-dir', self.server.state_dir, '--no-retro', 'fake queue fixture', *flags)

    def claim_during_enqueue(self, thread, notification, timeout):
        self.assertEqual(thread, self.operator)
        self.assertGreater(timeout, 0)
        command = shlex.split(notification.splitlines()[1])
        self.assertEqual(command[:-1],
                         ['python3', '~/.agents/skills/agent-bridge/scripts/bridge.py', 'close-operator'])
        mid = command[-1]
        message = self.server.call('show', id=mid)
        self.assertEqual(message['state'], 'dispatching')
        self.assertNotIn(self.target, notification)
        self.assertNotIn(self.cwd, notification)
        self.assertNotIn(message['body'], notification)
        self.assertNotIn('receive-codex', notification)
        self.assertIn('confirm the thread is local with no active turn (idle or notLoaded)', notification)
        self.assertIn('Call set_thread_archived once with the exact threadId, hostId local', notification)
        self.assertIn('--finish CLOSE_ID --expect-session UUID', notification)
        with patch.dict(os.environ, {'CODEX_THREAD_ID': self.target}):
            self.assertEqual(self.cli(*command[2:])[0], 1)
        with patch.dict(os.environ, {'CODEX_THREAD_ID': self.operator}):
            self.assertEqual(self.cli('receive-codex', mid), (1, {
                'error': 'The addressed message is a close handoff; claim it with close-operator.'}))
            self.assertEqual(self.cli(*command[2:]), (0, {
                'project': 'work', 'name': 'worker', 'session': self.target, 'cwd': self.cwd,
                'close_id': message['close_request']['close_id'],
            }))
            self.assertEqual(self.cli(*command[2:])[0], 1)

    def test_queue_receipt_preserves_an_early_close_operator_claim(self):
        def enqueue(thread, notification, timeout):
            self.claim_during_enqueue(thread, notification, timeout)
            return str(uuid.uuid4())

        with patch.object(codex_queue, 'enqueue', side_effect=enqueue) as submit:
            code, receipt = self.handoff()
            self.assertEqual((code, receipt['operator_delivery']), (1, 'in_progress'))
            self.assertIs(receipt['wake_requested'], True)
            self.assertEqual(self.handoff()[1]['operator_message_id'], receipt['operator_message_id'])
        submit.assert_called_once()
        self.wake.assert_called_once()
        self.assertEqual(self.wake.call_args.args[0], self.operator)
        self.assertEqual(self.server.call('show', id=receipt['operator_message_id'])['state'], 'in_progress')

    def test_early_claim_confirms_handoff_when_queue_receipt_is_lost(self):
        def enqueue(thread, notification, timeout):
            self.claim_during_enqueue(thread, notification, timeout)
            raise codex_live.DeliveryError(codex_queue.UNKNOWN, uncertain=True)

        with patch.object(codex_queue, 'enqueue', side_effect=enqueue) as submit:
            code, receipt = self.handoff()
        submit.assert_called_once()
        self.wake.assert_not_called()
        self.assertEqual((code, receipt['operator_delivery']), (1, 'in_progress'))
        self.assertNotIn('operator_error', receipt)

    def test_ambiguous_queue_without_operator_claim_is_never_replayed(self):
        error = codex_live.DeliveryError(codex_queue.UNKNOWN, uncertain=True)
        with patch.object(codex_queue, 'enqueue', side_effect=error) as submit:
            code, receipt = self.handoff()
            self.assertEqual((code, receipt['operator_delivery']), (1, 'uncertain'))
            self.assertEqual(self.handoff()[1]['operator_message_id'], receipt['operator_message_id'])
        submit.assert_called_once()
        message = self.server.call('show', id=receipt['operator_message_id'])
        with self.assertRaisesRegex(ValueError, 'cannot be delivered manually'):
            delivery.push_codex(self.server.path, message)

    def test_custom_broker_socket_does_not_use_desktop_queue(self):
        with patch.object(bridge.broker, 'DEFAULT_SOCKET', Path(self.cwd) / 'other.sock'), \
                patch.object(codex_queue, 'enqueue') as submit:
            code, receipt = self.handoff()
        self.assertEqual(code, 1)
        self.assertIn('Enrolled Codex close operator is unavailable', receipt['error'])
        route, = self.server.call('routes', project='work')
        self.assertNotIn('closing', route)
        submit.assert_not_called()

    def test_explicit_handoff_does_not_gain_desktop_queue_dispatch(self):
        with patch.object(codex_queue, 'enqueue') as submit:
            code, receipt = self.handoff('--handoff')
        # Without IPC an explicit handoff cannot reach the operator, so it is refused before the
        # route is reserved instead of stranding it (#196).
        self.assertEqual(code, 1)
        self.assertIn('Enrolled Codex close operator is unavailable', receipt['error'])
        route, = self.server.call('routes', project='work')
        self.assertNotIn('closing', route)
        submit.assert_not_called()

    def test_late_process_keeps_guarded_close_and_withholds_target_from_operator(self):
        with patch.object(codex_queue, 'enqueue', return_value=str(uuid.uuid4())):
            _, receipt = self.handoff()
        mid = receipt['operator_message_id']
        holder = (12345, 'shell', str(Path(self.cwd) / 'nested'))
        with patch.dict(os.environ, {'CODEX_THREAD_ID': self.operator}), \
                patch.object(codex_archive.wt, 'open_files', return_value=[holder]) as scan:
            self.assertEqual(self.cli('close-operator', mid), (1, {
                'error': 'A running process uses the Codex native close workspace. Closing route kept.'}))
        scan.assert_called_once()
        route, = self.server.call('routes', project='work')
        self.assertEqual(route['closing'], receipt['close_id'])
        self.assertEqual(self.server.call('show', id=mid)['state'], 'in_progress')

    def test_guarded_claim_requires_updated_operator_cli(self):
        with patch.object(codex_queue, 'enqueue', return_value=str(uuid.uuid4())):
            _, receipt = self.handoff()
        mid = receipt['operator_message_id']
        for guard in ({}, {'native_guard_version': True}, {'native_guard_version': 2}):
            with self.subTest(guard=guard):
                with self.assertRaisesRegex(ValueError, 'requires an updated close-operator CLI'):
                    self.server.call('close-operator', id=mid, operator_session=self.operator, **guard)
                self.assertEqual(self.server.call('show', id=mid)['state'], 'submitted')
        with patch.dict(os.environ, {'CODEX_THREAD_ID': self.operator}):
            code, target = self.cli('close-operator', mid)
        self.assertEqual((code, target['session']), (0, self.target))
        self.assertEqual(self.server.call('show', id=mid)['state'], 'in_progress')

    def test_process_scan_error_withholds_guarded_target(self):
        with patch.object(codex_queue, 'enqueue', return_value=str(uuid.uuid4())):
            _, receipt = self.handoff()
        with patch.dict(os.environ, {'CODEX_THREAD_ID': self.operator}), \
                patch.object(codex_archive.wt, 'open_files', side_effect=OSError('private diagnostics')):
            self.assertEqual(self.cli('close-operator', receipt['operator_message_id']), (1, {
                'error': 'Codex native close could not verify process working directories. Closing route kept.'}))


class DefaultDesktopFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name).resolve()
        self.state = Path.home() / '.codex' / codex_preview.STATE_FILE
        self.queue = Path.home() / '.codex' / 'queue_1.sqlite'
        self.signed, self.owned = [], []
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in OVERRIDES:
            os.environ.pop(name, None)

        def checked(argv, timeout=2):
            self.assertEqual(argv[:3], ['/usr/bin/codesign', '--verify', '--strict'])
            self.signed.append((argv[3], argv[4]))
            return ''

        for target, name, value in (
                (sys, 'platform', 'darwin'),
                (codex_queue, 'checked_output', checked),
                (codex_preview, 'state_path', lambda executable: self.state),
                (codex_preview, 'require_owned_store', self.owned.append),
                (codex_queue, 'desktop_queue_owner', lambda queue, read=None, **kwargs: ('4242', '77', HELPER))):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class QueueRouteTests(DefaultDesktopFixture):
    def setUp(self):
        super().setUp()
        self.route = {'transport': 'codex', 'session': str(uuid.uuid4()), 'cwd': str(self.cwd)}
        self.saved = (str(self.cwd), 0)
        patcher = patch.object(codex_preview, '_saved_thread_row', lambda route, path=None: self.saved)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_signed_store_with_the_saved_workspace_and_one_owner_passes(self):
        codex_queue.check_route(self.route)
        self.assertEqual(self.signed, [('-R=' + codex_queue.SIGNATURE, str(codex_queue.CLI_BUNDLE))])
        self.assertEqual(self.owned, [self.queue])

    def test_any_store_override_is_refused_before_a_process_runs(self):
        for name in OVERRIDES:
            with self.subTest(override=name), patch.dict(os.environ, {name: 'x'}):
                with self.assertRaisesRegex(codex_queue.QueueUnavailable, 'default Desktop configuration'):
                    codex_queue.check_route(self.route)
        self.assertEqual((self.signed, self.owned), ([], []))

    def test_archived_route_is_still_terminal(self):
        self.saved = (str(self.cwd), 1)
        with self.assertRaises(ThreadArchived):
            codex_queue.check_available(self.route)

    def cli(self, server, *args):
        with redirect_stdout(io.StringIO()) as stdout, patch('sys.stdin', io.StringIO('private body')):
            code = bridge.main(['--socket', server.path, *args])
        return code, json.loads(stdout.getvalue())

    def test_queue_causes_reach_send_deliver_and_the_stored_note(self):
        endpoint = codex_live.EndpointUnavailable
        removed = self.cwd / 'removed'
        removed.mkdir()
        cases = (
            ('executable', patch.dict(os.environ), 'requires the default route executable'),
            ('configuration', patch.dict(os.environ, {'CODEX_SQLITE_HOME': 'fixture'}),
             'requires the default Desktop configuration'),
            ('signature', patch.object(codex_queue, 'checked_output',
                                       side_effect=subprocess.CalledProcessError(1, 'private diagnostic')),
             'CLI signature check failed'),
            ('state-config', patch.object(codex_preview, 'state_path', side_effect=ValueError('private diagnostic')),
             'could not verify the native state store configuration'),
            ('state-store', patch.object(codex_preview, 'state_path', return_value=self.cwd / 'elsewhere.sqlite'),
             'requires the default native state store'),
            ('saved-thread', patch.object(codex_preview, '_saved_thread_row', side_effect=OSError('private diagnostic')),
             'could not verify the saved thread metadata'),
            ('workspace', patch.object(codex_preview, '_saved_thread_row', return_value=(str(removed), 0)),
             'saved workspace differs from the route cwd'),
            ('missing', patch.object(codex_preview, '_saved_thread_row', return_value=(str(removed), 0)),
             f"route cwd {removed} no longer exists; the Codex thread's saved cwd must exist for queue delivery"),
            ('store-owner', patch.object(codex_preview, 'require_owned_store', side_effect=OSError('private diagnostic')),
             'store is unavailable or is not owned and protected'),
            ('desktop-owner', patch.object(codex_queue, 'desktop_queue_owner', return_value=None),
             'No unique local Desktop queue owner. The queue holder could not be verified.'),
            ('cli-start', patch.object(codex_queue.subprocess, 'Popen', side_effect=OSError('private diagnostic')),
             'CLI could not be started'),
        )
        with (RunningBroker() as server,
              patch.object(bridge.broker, 'DEFAULT_SOCKET', Path(server.path)),
              patch.object(codex_live, 'exact_saved_workspace', return_value=(None, None)),
              patch.object(codex_live.Client, 'connect', side_effect=endpoint(endpoint.DEFAULT, endpoint.REFUSED))):
            server.call('register', project='p', name='sender', transport='realtime', cwd=str(self.cwd))
            for name, check, detail in cases:
                with self.subTest(cause=name), check:
                    route = dict(self.route, cwd=str(removed)) if name == 'missing' else dict(self.route)
                    if name == 'executable':
                        route['executable'] = '/opt/other/codex'
                    server.call('register', project='p', name=name, **route)
                    if name == 'missing':
                        removed.rmdir()
                    code, failure = self.cli(server, 'send', '--project', 'p', '--from', 'sender',
                                             '--to', name, '--key', name, '--file', '-')
                    self.assertEqual(code, 1)
                    self.assertIn(detail, failure['error'])
                    self.assertNotIn('IPC', failure['error'])
                    self.assertNotIn('private diagnostic', failure['error'])
                    mid = failure['message']['id']
                    stored = server.call('show', id=mid)
                    self.assertEqual((stored['state'], stored['note']), ('pending', failure['error']))
                    code, result = self.cli(server, 'deliver', mid)
                    self.assertEqual((code, result['error']), (1, failure['error']))

            code, routes = self.cli(server, 'routes', '--project', 'p', '--no-model')
            self.assertEqual(code, 0)
            self.assertEqual([route['name'] for route in routes if 'cwd_warning' in route], ['missing'])
            self.assertIn(str(removed), next(route['cwd_warning'] for route in routes if route['name'] == 'missing'))

    def test_existing_workspace_submits_the_same_notice_bytes(self):
        endpoint = codex_live.EndpointUnavailable
        with (RunningBroker() as server,
              patch.object(bridge.broker, 'DEFAULT_SOCKET', Path(server.path)),
              patch.object(codex_live, 'exact_saved_workspace', return_value=(None, None)),
              patch.object(codex_live.Client, 'connect', side_effect=endpoint(endpoint.DEFAULT, endpoint.REFUSED)),
              patch.object(codex_queue, 'enqueue', return_value=str(uuid.uuid4())) as enqueue,
              patch.object(codex_wake_open, 'request_queued_wake') as wake):
            server.call('register', project='p', name='sender', transport='realtime', cwd=str(self.cwd))
            server.call('register', project='p', name='desktop', **self.route)
            code, result = self.cli(server, 'send', '--project', 'p', '--from', 'sender',
                                    '--to', 'desktop', '--key', 'ok', '--file', '-')
            self.assertEqual((code, result['state'], result['delivery_path']), (0, 'submitted', codex_queue.DELIVERY_PATH))
            self.assertEqual(server.call('show', id=result['id'])['body'].encode(), b'private body')
            enqueue.assert_called_once()
            thread, notice, _ = enqueue.call_args.args
            self.assertEqual(thread, self.route['session'])
            self.assertEqual(notice.encode(), codex_queue.notice(result['id']).encode())
            wake.assert_called_once()


class WakePreflightTests(DefaultDesktopFixture):
    def setUp(self):
        super().setUp()
        self.app = self.cwd / 'Fake.app'
        (self.app / 'Contents').mkdir(parents=True)
        self.write_build(*codex_wake_open.APP_BUILD)
        patcher = patch.object(codex_queue, 'APP', self.app)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_build(self, version, build):
        with (self.app / 'Contents/Info.plist').open('wb') as output:
            plistlib.dump({'CFBundleShortVersionString': version, 'CFBundleVersion': build}, output)

    def test_verified_build_signatures_and_stores_return_the_owner(self):
        self.assertEqual(codex_wake_open.prepare(30),
                         codex_wake_open.Desktop(self.state, self.queue, ('4242', '77', HELPER)))
        self.assertEqual(self.signed, [('-R=' + codex_wake_open.APP_SIGNATURE, str(self.app)),
                                       ('-R=' + codex_queue.SIGNATURE, str(codex_queue.CLI_BUNDLE))])
        self.assertEqual(self.owned, [self.state, self.queue])

    def test_an_unverified_app_build_is_named_before_any_signature_check(self):
        self.write_build('99.0.1', '1')
        with self.assertRaisesRegex(codex_wake_open.WakeError, 'build has not been verified for queue wake'):
            codex_wake_open.prepare(30)
        self.assertEqual(self.signed, [])

    def test_overrides_bad_signatures_other_stores_and_missing_owners_share_the_context_refusal(self):
        refusals = []
        for name in OVERRIDES:
            with patch.dict(os.environ, {name: 'x'}):
                refusals.append(self.refusal())
        self.assertEqual((self.signed, self.owned), ([], []))
        with patch.object(codex_queue, 'checked_output', side_effect=subprocess.CalledProcessError(1, 'codesign')):
            refusals.append(self.refusal())
        with patch.object(codex_preview, 'state_path', lambda executable: self.cwd / 'elsewhere.sqlite'):
            refusals.append(self.refusal())
        with patch.object(codex_queue, 'desktop_queue_owner', lambda queue, read=None: None):
            refusals.append(self.refusal())
        refusals.append(self.refusal(timeout=codex_preview.CONFIG_TIMEOUT / 2))
        self.assertEqual(set(refusals), {codex_wake_open.CONTEXT_ERROR})

    def refusal(self, timeout=30):
        with self.assertRaises(codex_wake_open.WakeError) as raised:
            codex_wake_open.prepare(timeout)
        return str(raised.exception)


if __name__ == '__main__':
    unittest.main()
