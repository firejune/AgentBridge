import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

import agy_live
import bridge
import sessions
from support import RunningBroker
from test_agy_live import CSRF, FakeAgyServer
from test_peer import FakeClaude


def run(argv):
    out = io.StringIO()
    with mock.patch('sys.stdout', out):
        code = bridge.main(argv)
    return code, json.loads(out.getvalue())


class UnregisterTests(unittest.TestCase):
    def test_unregister_removes_route_and_survives_restart(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as cwd:
            server.call('register', project='p', name='w', cwd=cwd)
            server.call('register', project='p', name='keep', cwd=cwd)
            server.call('unregister', project='p', name='w')
            server.restart()
            self.assertEqual([r['name'] for r in server.call('routes', project='p')], ['keep'])
            with self.assertRaisesRegex(ValueError, "No participant 'w'"):
                server.call('unregister', project='p', name='w')

    def test_channel_owner_is_refused_by_name(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as cwd:
            server.call('register', project='p', name='w', cwd=cwd)
            server.call('channel-bind', project='p', name='view', owner='w', description='d', expect_generation=0)
            with self.assertRaisesRegex(ValueError, 'owns active channel'):
                server.call('unregister', project='p', name='w')
            self.assertEqual(len(server.call('routes', project='p')), 1)


class CloseClaudeTests(unittest.TestCase):
    IDE = {'entrypoint': 'claude-vscode'}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='abc-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.sid = str(uuid.uuid4())
        self.fakes = []

    def tearDown(self):
        for fake in self.fakes:
            fake.close()
        self.temp.cleanup()

    def fake(self, pid=None, extra=IDE):
        self.fakes.append(FakeClaude(self.temp.name, self.sid, pid=pid, extra=extra))

    def test_terminates_the_exact_ide_session_and_reports_resumable(self):
        child = subprocess.Popen(['sleep', '30'])
        self.fake(pid=child.pid)
        sent = []
        def kill(pid, sig):
            sent.append((pid, sig)); os.kill(pid, sig); child.wait()
        closed = sessions.close_claude(self.sid, sessions_dir=self.root/'sessions', killer=kill, poll=0.01)
        self.assertEqual((closed['closed'], closed['method'], closed['resumable']), (True, 'terminate', True))
        self.assertEqual(sent, [(child.pid, 15)])
        self.assertIn('code 143', closed['note'])

    def test_desktop_session_is_closable_with_its_own_note(self):
        child = subprocess.Popen(['sleep', '30'])
        self.fake(pid=child.pid, extra={'entrypoint': 'claude-desktop', 'hostSessionId': 'local_x'})
        def kill(pid, sig):
            os.kill(pid, sig); child.wait()
        closed = sessions.close_claude(self.sid, sessions_dir=self.root/'sessions', killer=kill, poll=0.01)
        self.assertEqual((closed['host'], closed['resumable']), ('claude-desktop', True))
        self.assertIn('session interrupted', closed['note'])

    def test_refuses_own_session_unknown_host_and_never_escalates(self):
        self.fake()
        kill = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'calling session itself'):
            sessions.close_claude(self.sid, own_session=self.sid, sessions_dir=self.root/'sessions', killer=kill)
        kill.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'still running'):
            sessions.close_claude(self.sid, sessions_dir=self.root/'sessions', killer=kill, timeout=0.05, poll=0.01)
        kill.assert_called_once()
        self.fakes.pop().close()
        (self.root/'sessions'/f'{os.getpid()}.json').unlink()
        self.fake(pid=os.getppid(), extra={'entrypoint': 'sdk-cli'})
        with self.assertRaisesRegex(ValueError, 'unsupported host sdk-cli'):
            sessions.close_claude(self.sid, sessions_dir=self.root/'sessions', killer=kill)
        kill.assert_called_once()


class CloseAgyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aba-', dir='/tmp')
        self.root = Path(self.temp.name).resolve()
        self.sid = str(uuid.uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def env(self, fake):
        return mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF})

    def test_close_agy_detaches_idle_conversation_without_archiving(self):
        with FakeAgyServer(self.root, self.sid) as fake, self.env(fake):
            closed = agy_live.close_agy(fake.other_sid)
        self.assertEqual(
            (closed['closed'], closed['method'], closed['host'], closed['resumable']),
            (True, 'detach', 'agy', True),
        )
        sent = [b for p, _, b in fake.requests if p.endswith('/UpdateConversationAnnotations')]
        self.assertEqual(sent, [])

    def test_close_agy_refuses_running_conversation(self):
        with FakeAgyServer(self.root, self.sid) as fake, self.env(fake):
            with self.assertRaisesRegex(ValueError, 'is running'):
                agy_live.close_agy(self.sid)


class ArchiveAgyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aba-', dir='/tmp')
        self.root = Path(self.temp.name).resolve()
        self.sid = str(uuid.uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def env(self, fake):
        return mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF})

    def test_archives_idle_conversation_and_reads_it_back(self):
        with FakeAgyServer(self.root, self.sid) as fake, self.env(fake):
            closed = agy_live.archive_agy(fake.other_sid)
        self.assertEqual((closed['method'], closed['restore_confirmed']), ('archive', False))
        self.assertEqual(closed['archived_at'], '2026-10-24T09:16:35Z')
        sent = [b for p, _, b in fake.requests if p.endswith('/UpdateConversationAnnotations')]
        self.assertEqual(sent, [{'cascadeId': fake.other_sid, 'annotations': {'archived': True}, 'mergeAnnotations': True}])

    def test_running_is_refused_and_missing_readback_is_uncertain(self):
        with FakeAgyServer(self.root, self.sid) as fake, self.env(fake):
            with self.assertRaisesRegex(ValueError, 'is running'):
                agy_live.archive_agy(self.sid)
            fake.ignore_archive = True
            with self.assertRaises(agy_live.ArchiveUncertain):
                agy_live.archive_agy(fake.other_sid)
        sent = [b for p, _, b in fake.requests if p.endswith('/UpdateConversationAnnotations')]
        self.assertEqual(len(sent), 1)

    def test_failed_archive_request_is_uncertain_and_not_resent(self):
        with FakeAgyServer(self.root, self.sid) as fake, self.env(fake):
            real = agy_live.rpc_call
            def flaky(url, csrf, method, payload, timeout=5):
                if method == 'UpdateConversationAnnotations':
                    raise agy_live.DeliveryError('timed out')
                return real(url, csrf, method, payload, timeout)
            with mock.patch.object(agy_live, 'rpc_call', side_effect=flaky) as calls:
                with self.assertRaisesRegex(agy_live.ArchiveUncertain, 'outcome is unknown'):
                    agy_live.archive_agy(fake.other_sid)
        updates = [c for c in calls.call_args_list if c.args[2] == 'UpdateConversationAnnotations']
        self.assertEqual(len(updates), 1)


class CloseCommandTests(unittest.TestCase):
    def test_codex_and_agy_detach_without_archiving_and_remove_route(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as cwd, \
                FakeAgyServer(cwd, str(uuid.uuid4())) as fake, \
                mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF}):
            codex_sid = str(uuid.uuid4())
            server.call('register', project='p', name='n', cwd=cwd, transport='codex', session=codex_sid)
            code, out = run(['--socket', server.path, 'close', 'n', '--project', 'p'])
            self.assertEqual(
                (code, out['closed'], out['method'], out['resumable'], out['route_removed']),
                (0, True, 'detach', True, True),
            )
            self.assertEqual(server.call('routes', project='p'), [])

            server.call('register', project='p', name='j', cwd=cwd, transport='agy', session=fake.other_sid)
            code_j, out_j = run(['--socket', server.path, 'close', 'j', '--project', 'p'])
            self.assertEqual(
                (code_j, out_j['closed'], out_j['method'], out_j['host'], out_j['resumable'], out_j['route_removed']),
                (0, True, 'detach', 'agy', True, True),
            )
            self.assertEqual(server.call('routes', project='p'), [])
            sent = [b for p, _, b in fake.requests if p.endswith('/UpdateConversationAnnotations')]
            self.assertEqual(sent, [])

    def test_realtime_route_is_removed_and_old_broker_is_reported(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as cwd:
            server.call('register', project='p', name='w', cwd=cwd)
            server.call('register', project='p', name='k', cwd=cwd)
            code, out = run(['--socket', server.path, 'close', 'w', '--project', 'p'])
            self.assertEqual((code, out['closed'], out['route_removed']), (0, False, True))
            code, out = run(['--socket', server.path, 'close', 'k', '--project', 'p', '--keep-route'])
            self.assertEqual((code, out['route_removed']), (0, False))
            real = bridge.broker.request
            def old(path, args, timeout=10):
                if args['op'] == 'unregister':
                    raise ValueError('Unknown operation')
                return real(path, args, timeout)
            with mock.patch.object(bridge.broker, 'request', side_effect=old):
                code, out = run(['--socket', server.path, 'close', 'k', '--project', 'p'])
            self.assertEqual((code, out['route_removed']), (0, False))
            self.assertIn('restart it', out['route_note'])
            code, out = run(['--socket', server.path, 'close', 'gone', '--project', 'p'])
            self.assertEqual(code, 1)


if __name__ == '__main__':
    unittest.main()
