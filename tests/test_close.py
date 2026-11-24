from contextlib import closing
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
import agy_live
import codex_live
import codex_archive
import codex_preview
import sessions
from support import RunningBroker
from test_agy_live import CSRF, FakeAgyServer
from test_peer import FakeClaude


def dispatch_raises(error):
    """A deliver stub: the operator preflight's dry run passes, the real dispatch raises."""
    def deliver(*args, dry_run=False, **kwargs):
        if dry_run:
            return {'sent': False}
        raise error
    return deliver


def dispatches(deliver):
    return sum(not call.kwargs.get('dry_run', False) for call in deliver.call_args_list)


def run(argv):
    out = io.StringIO()
    with mock.patch('sys.stdout', out):
        code = bridge.main(argv)
    return code, json.loads(out.getvalue())


class FakeClock:
    """A monotonic clock that advances only when sleep is called; on_sleep may act."""
    def __init__(self, on_sleep=None):
        self.now, self.on_sleep, self.sleeps = 1000.0, on_sleep, 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self)


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

    def test_desktop_session_is_closable_with_its_own_note_and_optional_archive(self):
        child = subprocess.Popen(['sleep', '30'])
        self.fake(pid=child.pid, extra={'entrypoint': 'claude-desktop', 'hostSessionId': 'local_1234'})
        def kill(pid, sig):
            os.kill(pid, sig); child.wait()
        closed = sessions.close_claude(self.sid, sessions_dir=self.root/'sessions', killer=kill, poll=0.01)
        self.assertEqual((closed['host'], closed['archived'], closed['resumable']), ('claude-desktop', False, True))
        self.assertIn('session interrupted', closed['note'])

        # With archive=True, marks isArchived=True in the matching local_*.json metadata.
        self.fakes.pop().close()
        child2 = subprocess.Popen(['sleep', '30'])
        self.fake(pid=child2.pid, extra={'entrypoint': 'claude-desktop', 'hostSessionId': 'local_5678'})
        desktop_dir = self.root / 'claude-desktop-sessions' / 'acct' / 'org'
        desktop_dir.mkdir(parents=True)
        meta_file = desktop_dir / 'local_5678.json'
        clock = FakeClock()
        meta_file.write_text(
            json.dumps({'sessionId': 'local_5678', 'cliSessionId': self.sid, 'isArchived': False}),
            encoding='utf-8',
        )
        def kill2(pid, sig):
            os.kill(pid, sig); child2.wait()
        archived = sessions.close_claude(
            self.sid,
            sessions_dir=self.root/'sessions',
            killer=kill2,
            poll=0.01,
            archive=True,
            desktop_dir=desktop_dir,
            clock=clock,
            sleep=clock.sleep,
        )
        self.assertEqual(
            (archived['closed'], archived['method'], archived['archived'], archived['host']),
            (True, 'archive', True, 'claude-desktop'),
        )
        self.assertNotIn('archive_incomplete', archived)
        self.assertIn('still read 5.0s after the write', archived['note'])
        self.assertGreaterEqual(clock.now - 1000.0, sessions.ARCHIVE_SETTLE_SECONDS)
        self.assertIs(json.loads(meta_file.read_text(encoding='utf-8'))['isArchived'], True)

    def desktop_archive(self, on_sleep=None, metadata=None):
        """Close a desktop fake with archive=True; on_sleep(meta_file, clock) simulates the app."""
        child = subprocess.Popen(['sleep', '30'])
        self.fake(pid=child.pid, extra={'entrypoint': 'claude-desktop', 'hostSessionId': 'local_9abc'})
        desktop_dir = self.root / 'claude-desktop-sessions' / 'acct' / 'org'
        desktop_dir.mkdir(parents=True)
        meta_file = desktop_dir / 'local_9abc.json'
        meta_file.write_text(json.dumps(metadata or {'sessionId': 'local_9abc', 'cliSessionId': self.sid,
                                                     'isArchived': False}), encoding='utf-8')
        clock = FakeClock(on_sleep and (lambda clock: on_sleep(meta_file, clock)))
        def kill(pid, sig):
            os.kill(pid, sig); child.wait()
        result = sessions.close_claude(self.sid, sessions_dir=self.root/'sessions', killer=kill, poll=0.01,
                                       archive=True, desktop_dir=desktop_dir, clock=clock, sleep=clock.sleep)
        return result, meta_file, clock

    def test_desktop_archive_overwritten_by_the_app_during_the_settle_window_is_unconfirmed(self):
        def app_saves(meta_file, clock):
            if clock.sleeps == 2:  # the app's debounced save lands one second in
                meta_file.write_text(json.dumps({'sessionId': 'local_9abc', 'cliSessionId': self.sid,
                                                 'isArchived': False}), encoding='utf-8')
        result, _, clock = self.desktop_archive(app_saves)
        self.assertEqual((result['closed'], result['archived'], result['status'], result['archive_incomplete']),
                         (True, False, 'archive_unconfirmed', True))
        self.assertIn('local_9abc.json read isArchived=false 1.0s after the write', result['note'])
        self.assertLess(clock.now - 1000.0, sessions.ARCHIVE_SETTLE_SECONDS)  # stops at the first lost read

    def test_desktop_archive_with_unreadable_metadata_is_unconfirmed(self):
        def app_truncates(meta_file, clock):
            meta_file.write_text('{', encoding='utf-8')
        result, _, _ = self.desktop_archive(app_truncates)
        self.assertEqual((result['archived'], result['status']), (False, 'archive_unconfirmed'))
        self.assertIn('local_9abc.json was unreadable 0.5s after the write', result['note'])

        self.fakes.pop().close()
        (self.root / 'sessions').rename(self.root / 'sessions-old')
        (self.root / 'claude-desktop-sessions').rename(self.root / 'desktop-old')
        def app_drops_the_key(meta_file, clock):
            meta_file.write_text(json.dumps({'sessionId': 'local_9abc'}), encoding='utf-8')
        result, _, _ = self.desktop_archive(app_drops_the_key)
        self.assertEqual((result['archived'], result['status']), (False, 'archive_unconfirmed'))
        self.assertIn('read isArchived=null', result['note'])

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

    def test_archive_and_close_succeed_when_workspace_folder_is_deleted_or_absent(self):
        gone_dir = self.root / 'deleted-worktree'
        gone_dir.mkdir()
        gone_sid = str(uuid.uuid4())
        no_ws_sid = str(uuid.uuid4())
        with FakeAgyServer(self.root, self.sid) as fake, self.env(fake):
            fake.trajectories[gone_sid] = {
                'summary': 'Deleted Folder Session',
                'status': 'CASCADE_RUN_STATUS_IDLE',
                'workspaces': [{'workspaceFolderAbsoluteUri': gone_dir.as_uri()}],
                'lastModifiedTime': '2026-11-24T01:00:00Z',
            }
            fake.trajectories[no_ws_sid] = {
                'summary': 'No Workspace Session',
                'status': 'CASCADE_RUN_STATUS_IDLE',
                'workspaces': [],
                'lastModifiedTime': '2026-11-24T01:00:00Z',
            }
            gone_dir.rmdir()
            self.assertFalse(gone_dir.exists())
            self.assertTrue(agy_live.close_agy(gone_sid)['closed'])
            self.assertTrue(agy_live.archive_agy(gone_sid)['archived'])
            self.assertTrue(agy_live.close_agy(no_ws_sid)['closed'])
            self.assertTrue(agy_live.archive_agy(no_ws_sid)['archived'])
            # Discovery / delivery resolution still requires a workspace directory.
            with self.assertRaisesRegex(agy_live.DeliveryError, 'has no workspace directory'):
                agy_live.read_agy(no_ws_sid)


class CloseCodexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ab-close-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.db = self.home / 'state_5.sqlite'
        self.cwd = self.home / 'workspace'
        self.cwd.mkdir()
        self.sid = str(uuid.uuid4())
        self.rollout = self.home / 'sessions' / f'{self.sid}.jsonl'
        self.archive = self.home / 'archived_sessions' / self.rollout.name
        self.rollout.parent.mkdir()
        self.archive.parent.mkdir()
        self.rollout.write_text('test transcript', encoding='utf-8')
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER, rollout_path TEXT, '
                       "title TEXT DEFAULT 'fixture', updated_at INTEGER DEFAULT 1, archived_at INTEGER)")
            db.execute('INSERT INTO threads (id, cwd, archived, rollout_path) VALUES (?, ?, 0, ?)',
                       (self.sid, str(self.cwd), str(self.rollout)))
            db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)')
        executable = self.home / 'fake-codex'
        executable.write_text(
            '#!/usr/bin/env python3\n'
            'import json, sys\n'
            'for line in sys.stdin:\n'
            '    request = json.loads(line)\n'
            '    if "id" not in request:\n'
            '        continue\n'
            '    method = request["method"]\n'
            '    result = {"initialize": {}, "config/read": {"config": {}}, '
            '"configRequirements/read": {"requirements": None}}[method]\n'
            '    print(json.dumps({"id": request["id"], "result": result}), flush=True)\n',
            encoding='utf-8')
        executable.chmod(0o700)
        self.executable = str(executable)
        env = mock.patch.dict(os.environ, {'CODEX_HOME': str(self.home), 'CODEX_SQLITE_HOME': str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.server = RunningBroker()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.register('target')

    def register(self, name):
        return self.server.call('register', project='p', name=name, cwd=str(self.cwd), transport='codex',
                                session=self.sid, executable=self.executable)

    def close(self, *flags):
        retro = [] if '--finish' in flags else ['--no-retro', 'native archive fixture']
        return run(['--socket', self.server.path, 'close', 'target', '--project', 'p', *retro, *flags])

    def test_finish_and_handoff_flag_refusals_name_the_flag(self):
        close_id = str(uuid.uuid4())
        cases = [
            (('--finish', close_id), '--finish requires --expect-session'),
            (('--finish', close_id, '--expect-session', self.sid, '--keep-route'),
             '--finish removes the verified route and cannot be combined with --keep-route'),
            (('--finish', close_id, '--expect-session', self.sid, '--handoff'),
             '--finish completes a prepared handoff and cannot be combined with --handoff'),
            (('--handoff',), '--handoff requires --archive'),
            (('--archive', '--handoff', '--keep-route'),
             '--handoff removes the verified route and cannot be combined with --keep-route'),
        ]
        for flags, expected in cases:
            with self.subTest(flags=flags):
                self.assertEqual(self.close(*flags), (1, {'error': expected}))
        self.server.call('register', project='p', name='plain', cwd=str(self.cwd))
        self.assertEqual(run(['--socket', self.server.path, 'close', 'plain', '--project', 'p', '--no-retro', 'x',
                              '--archive', '--handoff']), (1, {'error': '--handoff requires a Codex route'}))
        route, = [row for row in self.server.call('routes', project='p') if row['name'] == 'target']
        self.assertNotIn('closing', route)

    def enroll(self, session=None, *, prefer_native=None):
        self.operator_sid = session or str(uuid.uuid4())
        self.server.call('register', project='ops', name='archive', cwd=str(self.cwd), transport='codex',
                         session=self.operator_sid, executable=self.executable)
        self.server.call('register', project='ops', name='trigger', cwd=str(self.cwd))
        config = Path(self.server.state_dir) / 'close-operator.json'
        settings = {'project': 'ops', 'name': 'archive', 'session': self.operator_sid, 'sender': 'trigger'}
        if prefer_native is not None:
            settings['prefer_native_for_direct'] = prefer_native
        config.write_text(json.dumps(settings), encoding='utf-8')
        config.chmod(0o600)

    def add_descendant(self):
        sid = str(uuid.uuid4())
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO threads (id, cwd, archived) VALUES (?, ?, 0)', (sid, str(self.cwd)))
            db.execute('INSERT INTO thread_spawn_edges VALUES (?, ?)', (self.sid, sid))
        return sid

    def archive_flags(self):
        with closing(sqlite3.connect(self.db)) as db:
            return dict(db.execute('SELECT id, archived FROM threads').fetchall())

    def remove_descendant(self, sid):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DELETE FROM thread_spawn_edges WHERE child_thread_id=?', (sid,))
            db.execute('DELETE FROM threads WHERE id=?', (sid,))

    def saved_archive(self, method, params):
        # A native archive: the bit, and for the route's thread the rollout moved to archived_sessions.
        self.assertEqual(method, 'thread/archive')
        if params['threadId'] == self.sid:
            self.rollout.rename(self.archive)
            self.record_archive(self.archive)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE threads SET archived=1, archived_at=2 WHERE id=?', (params['threadId'],))
        return {}

    def direct_archive(self, behavior, *flags):
        Path(self.server.state_dir, 'policy.json').write_text(
            json.dumps({'codex_self_archive': False}), encoding='utf-8')
        with mock.patch.object(codex_archive, 'executable', return_value=self.executable), \
                mock.patch.object(codex_preview, 'state_path', return_value=self.db), \
                mock.patch.object(codex_archive.wt, 'open_files', return_value=[]), \
                mock.patch.object(sessions, 'CodexClient') as client:
            client.return_value.__enter__.return_value.call.side_effect = behavior
            return self.close('--archive', '--state-dir', self.server.state_dir, *flags)

    def operator(self, mid, session):
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': session}):
            return run(['--socket', self.server.path, 'close-operator', mid])

    def record_archive(self, rollout):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE threads SET archived=1, rollout_path=? WHERE id=?', (str(rollout), self.sid))

    def test_close_handoff_persists_and_only_finishes_after_exact_archive(self):
        code, prepared = self.close('--archive', '--handoff')
        self.assertEqual((code, prepared['status'], prepared['closed'], prepared['route_removed']),
                         (0, 'awaiting_native_archive', False, False))
        self.assertEqual(prepared['session'], self.sid)
        self.server.restart()
        status, recovered = self.close('--archive', '--handoff')
        self.assertEqual(status, 0)
        self.assertEqual(recovered, {k: v for k, v in prepared.items()
                                     if k not in ('retro_id', 'store', 'notification')})
        route, = self.server.call('routes', project='p')
        self.assertEqual(route['closing'], prepared['close_id'])
        finish = ('--finish', prepared['close_id'], '--expect-session')
        for wrong in (str(uuid.uuid4()), self.sid):
            with self.subTest(session=wrong):
                code, result = self.close(*finish, wrong)
                self.assertEqual(code, 1)
                self.assertIn('error', result)
        self.record_archive(self.rollout)  # A flag-only archive is not restorable.
        self.assertEqual(self.close(*finish, self.sid)[0], 1)
        self.record_archive(self.archive)  # A missing archive file is also insufficient.
        self.assertEqual(self.close(*finish, self.sid)[0], 1)
        self.assertEqual(self.server.call('routes', project='p')[0]['closing'], prepared['close_id'])
        with mock.patch.object(codex_archive, 'close') as direct:
            self.assertEqual(self.close('--archive')[0], 1)
            direct.assert_not_called()
        self.rollout.rename(self.archive)
        code, result = self.close(*finish, self.sid)
        self.assertEqual((code, result['closed'], result['archived'], result['route_removed']), (0, True, True, True))
        self.assertEqual(self.server.call('routes', project='p'), [])
        self.assertEqual(self.archive.read_text(encoding='utf-8'), 'test transcript')

    def test_close_refuses_alias_channel_and_unfinished_delivery(self):
        self.register('alias')
        self.assertIn('another registered route', self.close('--archive', '--handoff')[1]['error'])
        self.server.call('unregister', project='p', name='alias')
        self.server.call('channel-bind', project='p', name='view', owner='target', description='view')
        self.assertIn('active channels', self.close('--archive', '--handoff')[1]['error'])
        self.server.call('channel-close', project='p', name='view', expect_generation=1)
        self.server.call('register', project='p', name='sender', cwd=str(self.cwd))
        pending = self.server.call('send', project='p', sender='sender', recipient='target', key='one', body='work')
        self.assertIn('unfinished correspondence', self.close('--archive', '--handoff')[1]['error'])
        self.server.call('claim', id=pending['id'])
        error = self.close('--archive', '--handoff')[1]['error']
        self.assertIn('active delivery', error)
        self.assertIn(pending['id'], error)
        self.server.call('finish', id=pending['id'], state='submitted')
        self.server.call('progress', id=pending['id'], note='working')
        self.assertIn('unfinished correspondence', self.close('--archive', '--handoff')[1]['error'])
        self.server.call('resolve', id=pending['id'], state='done', note='reviewed')
        self.assertEqual(self.close('--archive', '--handoff')[0], 0)

    def test_close_finishes_received_one_way_and_reply_but_lists_work_still_open(self):
        self.server.call('register', project='p', name='peer', cwd=str(self.cwd))

        def send(key, sender='peer', recipient='target', **intent):
            return self.server.call('send', project='p', sender=sender, recipient=recipient,
                                    key=key, body='fixture', **intent)['id']

        def finish(mid, state):
            self.server.call('claim', id=mid)
            self.server.call('finish', id=mid, state=state)

        received_one_way = send('one-way', one_way=True)
        report_parent = send('request')
        received_reply = self.server.call('reply', id=report_parent, key='report', body='fixture')['id']
        for mid in (received_one_way, received_reply):
            finish(mid, 'submitted')
            self.server.call('ack', id=mid)

        received_request = send('read-request')
        unread_one_way = send('unread', one_way=True)
        working_one_way = send('working', one_way=True)
        unread_parent = send('other-request', sender='target', recipient='peer')
        unread_reply = self.server.call('reply', id=unread_parent, key='unread-report', body='fixture')['id']
        failed = send('failed')
        done = send('done')
        for mid in (received_request, unread_one_way):
            finish(mid, 'submitted')
        self.server.call('ack', id=received_request)
        self.server.call('progress', id=working_one_way)
        finish(failed, 'failed')
        self.server.call('resolve', id=done, state='done', note='reviewed')

        blocking = (received_request, unread_one_way, working_one_way, unread_reply)
        code, receipt = self.close('--archive', '--handoff')
        self.assertEqual(code, 1)
        self.assertEqual(receipt['error'],
                         'This Codex thread has unfinished correspondence; reconcile it before closing '
                         f"(IDs: {', '.join(sorted(blocking))})")
        self.assertEqual(self.server.call('show', id=report_parent)['state'], 'answered')
        for mid in blocking:
            self.server.call('resolve', id=mid, state='done', note='reviewed')
        self.assertEqual(self.close('--archive', '--handoff')[0], 0)
        for mid in (received_one_way, received_reply):
            self.assertEqual(self.server.call('show', id=mid)['state'], 'received')

    def test_close_finishes_submitted_direct_push_replies_from_its_sender(self):
        replies = {}
        for transport in ('claude-peer', 'agy', 'codex', 'realtime'):
            route = {'session': str(uuid.uuid4())} if transport != 'realtime' else {}
            self.server.call('register', project='p', name=transport, cwd=str(self.cwd),
                             transport=transport, **route)
            parent = self.server.call('send', project='p', sender=transport, recipient='target',
                                      key=f'ask-{transport}', body='request')
            reply = self.server.call('reply', id=parent['id'], key=f'reply-{transport}', body='report')
            replies[transport] = reply['id']

        def submit(mid, **fields):
            self.server.call('claim', id=mid)
            self.server.call('finish', id=mid, state='submitted', event='pushed', **fields)

        ordinary = self.server.call('send', project='p', sender='target', recipient='claude-peer',
                                    key='ordinary', body='work')['id']
        submit(ordinary)
        submit(replies['codex'], codex_queue_accepted=True)
        submit(replies['realtime'])
        direct = [replies[transport] for transport in ('claude-peer', 'agy')]
        blocking = [ordinary, replies['codex'], replies['realtime']]

        def check_blocking(ids):
            code, receipt = self.close('--archive', '--handoff')
            self.assertEqual(code, 1)
            self.assertEqual(receipt['error'],
                             'This Codex thread has unfinished correspondence; reconcile it before closing '
                             f"(IDs: {', '.join(sorted(ids))})")

        check_blocking(blocking + direct)
        for mid in direct:
            submit(mid)
        check_blocking(blocking)
        for mid in blocking:
            self.server.call('resolve', id=mid, state='done', note='reviewed')
        self.assertEqual(self.close('--archive', '--handoff')[0], 0)
        for mid in direct:
            self.assertEqual(self.server.call('show', id=mid)['state'], 'submitted')

    def test_close_handoff_retains_uncertain_delivery_after_restart_and_missing_workspace(self):
        self.server.call('register', project='p', name='sender', cwd=str(self.home))
        fields = dict(project='p', sender='sender', recipient='target', key='old', body='work')
        pending = self.server.call('send', **fields)
        mid = pending['id']
        self.server.call('claim', id=mid)
        self.server.restart()
        before = self.server.call('show', id=mid)
        self.assertEqual(before['state'], 'uncertain')
        self.cwd.rmdir()
        code, prepared = self.close('--archive', '--handoff')
        self.assertEqual((code, prepared['status']), (0, 'awaiting_native_archive'))
        with self.assertRaisesRegex(ValueError, 'Only pending messages'):
            self.server.call('claim', id=mid)
        self.rollout.rename(self.archive)
        self.record_archive(self.archive)
        code, result = self.close('--finish', prepared['close_id'], '--expect-session', self.sid)
        self.assertEqual((code, result['closed'], result['archived'], result['route_removed']), (0, True, True, True))
        self.assertEqual(result['correspondence_cleanup'],
                         dict(removed=0, retained=1, complete=False, active_retained=0))
        self.assertNotIn('cleanup_error', result)
        self.assertEqual(self.server.call('show', id=mid), before)
        self.assertEqual(self.server.call('send', **fields)['id'], mid)
        self.server.restart()
        # Runtime annotations are outside the correspondence checkpoint.
        saved = self.server.call('show', id=mid)
        self.assertEqual({k: v for k, v in saved.items() if k != 'note'},
                         {k: v for k, v in before.items() if k != 'note'})
        self.assertEqual(self.server.call('send', **fields)['id'], mid)

    def test_saved_state_release_is_removed_and_writes_nothing(self):
        # Agent Bridge never writes Codex state (#212): the flag only reports that it is gone.
        code, prepared = self.close('--archive', '--handoff')
        self.assertEqual((code, prepared['status']), (0, 'awaiting_native_archive'))
        before = self.db.read_bytes()
        for flags in (('--finish', prepared['close_id'], '--expect-session', self.sid, '--saved-state'),
                      ('--archive', '--saved-state')):
            with self.subTest(flags=flags):
                code, result = self.close(*flags)
                self.assertEqual(code, 1)
                self.assertIn('saved-state release was removed (#212)', result['error'])
                self.assertIn('natively', result['error'])
        # An older CLI that still sends the field is refused by the broker too.
        with self.assertRaisesRegex(ValueError, 'removed \\(#212\\)'):
            self.server.call('close-finish', project='p', name='target', close_id=prepared['close_id'],
                             session=self.sid, timeout=5, saved_state=True)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(self.rollout.is_file())
        route, = self.server.call('routes', project='p')
        self.assertEqual(route['closing'], prepared['close_id'])

    def test_direct_close_keeps_the_route_when_desktop_holds_the_thread(self):
        def held(method, params):
            raise sessions.CodexRpcError(method, {
                'code': -32600, 'message': f"thread {params['threadId']} already has an active writer"})

        before = self.db.read_bytes()
        code, result = self.direct_archive(held)
        self.assertEqual(code, 1)
        self.assertEqual((result['closed'], result['archived'], result['route_removed']), (False, False, False))
        self.assertEqual((result['results'][0]['status'], result['results'][0]['reason']),
                         ('not_archived', 'desktop_holds_thread'))
        self.assertEqual(self.db.read_bytes(), before)  # No write to Codex state.
        self.assertTrue(self.rollout.is_file())
        route, = self.server.call('routes', project='p')
        self.assertEqual(route['session'], self.sid)

    def test_direct_close_reports_a_bit_only_archive_as_not_closed(self):
        def bit_only(method, params):
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute('UPDATE threads SET archived=1, archived_at=2 WHERE id=?', (params['threadId'],))
            return {}

        code, result = self.direct_archive(bit_only)
        self.assertEqual(code, 1)
        self.assertEqual((result['closed'], result['archived'], result['route_removed']), (False, False, False))
        self.assertEqual(result['results'][0]['reason'], 'rollout_not_archived')
        self.assertTrue(self.rollout.is_file())
        self.assertEqual(len(self.server.call('routes', project='p')), 1)

    def test_explicit_handoff_refuses_an_unavailable_operator_before_reserving(self):
        self.enroll()
        with mock.patch.object(codex_live, 'deliver', side_effect=codex_live.ThreadArchived()) as deliver:
            code, result = self.close('--archive', '--handoff')
        self.assertEqual(code, 1)
        self.assertIn('Enrolled Codex close operator is unavailable', result['error'])
        self.assertTrue(deliver.call_args.kwargs['dry_run'])
        self.assertFalse(deliver.call_args.kwargs['allow_queue'])  # Explicit handoffs dispatch over IPC.
        route, = [row for row in self.server.call('routes', project='p') if row['name'] == 'target']
        self.assertNotIn('closing', route)

    def test_ping_reports_operator_health_and_closing_routes(self):
        ping = lambda: run(['--socket', self.server.path, 'ping'])[1]
        with mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(bridge, 'codex_delivery_health', return_value={'path': 'ipc', 'reason': None}):
            self.assertEqual(ping()['close_operator'], {'state': 'none', 'reason': None})
            self.enroll(prefer_native=False)
            with mock.patch.object(codex_live, 'deliver', side_effect=codex_live.ThreadArchived()):
                self.assertEqual(ping()['close_operator'],
                                 {'state': 'unavailable', 'reason': 'operator chat is archived'})
            with mock.patch.object(codex_live, 'deliver', return_value={'sent': False}):
                result = ping()
                self.assertEqual(result['close_operator'], {'state': 'ok', 'reason': None})
                self.assertEqual(result['closing_routes'], 0)
                self.close('--archive', '--handoff')
                self.assertEqual(ping()['closing_routes'], 1)

    def test_finish_reports_operator_receipt_failure_after_archive(self):
        close_id = str(uuid.uuid4())
        args = ['close', 'target', '--project', 'p', '--finish', close_id, '--expect-session', self.sid]
        receipt = {'route_removed': True, 'session': self.sid, 'closing': close_id,
                   'operator_receipt': 'needs_reconciliation', 'operator_error': 'fixture receipt failure'}
        with mock.patch.object(bridge.broker, 'request', return_value=receipt):
            code, result = run(args)
        self.assertEqual((code, result['archived'], result['route_removed']), (1, True, True))
        self.assertEqual(result['operator_receipt'], 'needs_reconciliation')
        self.assertEqual(result['operator_error'], 'fixture receipt failure')

    def test_closing_blocks_delivery_and_route_changes_but_preserves_history(self):
        self.server.call('register', project='p', name='sender', cwd=str(self.cwd))
        fields = dict(project='p', sender='sender', recipient='target', key='old', body='done')
        old = self.server.call('send', **fields)
        self.server.call('claim', id=old['id'])
        self.server.call('finish', id=old['id'], state='failed')
        self.assertEqual(self.close('--archive', '--handoff')[0], 0)
        self.assertEqual(self.server.call('send', **fields)['id'], old['id'])
        with self.assertRaisesRegex(ValueError, 'route is closing'):
            self.server.call('send', **{**fields, 'key': 'new'})
        with self.assertRaisesRegex(ValueError, 'route is closing'):
            self.server.call('reply', id=old['id'], key='reply', body='reply')
        with self.assertRaisesRegex(ValueError, 'route is closing'):
            self.server.call('resolve', id=old['id'], state='pending', note='checked')
        self.assertEqual(self.server.call('show', id=old['id'])['state'], 'failed')
        with self.assertRaisesRegex(ValueError, 'route is closing'):
            self.register('alias')
        with self.assertRaisesRegex(ValueError, 'route is closing'):
            self.server.call('unregister', project='p', name='target')
        self.assertEqual(self.close()[0], 1)
        self.assertEqual(self.close('--no-archive')[0], 1)

    def test_explicit_detach_preserves_original_rollout(self):
        code, detached = self.close('--no-archive', '--keep-route')
        self.assertEqual((code, detached['method'], detached['archived'], detached['route_removed']),
                         (0, 'detach', False, False))
        code, detached = self.close('--no-archive')
        self.assertEqual((code, detached['route_removed']), (0, True))
        self.assertTrue(self.rollout.is_file())

    def test_enrolled_operator_receives_one_request_and_claims_exact_close(self):
        self.enroll()
        with mock.patch.object(codex_live, 'deliver', return_value={'delivery_mode': 'idle', 'turn_id': 'test'}) as deliver:
            code, prepared = self.close('--archive', '--handoff')
            mid = prepared['operator_message_id']
            self.assertEqual((code, prepared['operator_delivery'], prepared['route_removed']),
                             (0, 'submitted', False))
            self.assertEqual(deliver.call_args.args[0]['session'], self.operator_sid)
            self.assertIn('close-operator ' + mid, deliver.call_args.args[2])
            self.assertEqual(shlex.split(deliver.call_args.args[2].splitlines()[1]),
                             [sys.executable, str(Path(bridge.__file__).resolve()),
                              '--socket', self.server.path, 'close-operator', mid])
            status, recovered = self.close('--archive', '--handoff')
            self.assertEqual(status, 0)
            self.assertEqual(recovered, {k: v for k, v in prepared.items()
                                         if k not in ('retro_id', 'store', 'notification')})
            self.server.restart()
            status, recovered = self.close('--archive', '--handoff')
            self.assertEqual(status, 0)
            self.assertEqual(recovered, {k: v for k, v in prepared.items()
                                         if k not in ('retro_id', 'store', 'notification')})
            # One preflight dry run, then exactly one dispatch however often the handoff is repeated.
            self.assertEqual([call.kwargs.get('dry_run', False) for call in deliver.call_args_list], [True, False])
        with self.assertRaisesRegex(ValueError, 'reserved'):
            self.server.call('send', project='ops', sender='trigger', recipient='archive',
                             key='agent-bridge-close:' + prepared['close_id'], body='spoof')
        forged = self.server.call('send', project='ops', sender='trigger', recipient='archive',
                                  key='ordinary', body='copy of archive notification')
        self.assertEqual(self.operator(forged['id'], self.operator_sid)[0], 1)
        self.assertEqual(self.operator(mid, self.sid)[0], 1)
        claimed = self.server.call('close-operator', id=mid, operator_session=self.operator_sid)
        target = claimed['target']
        self.assertEqual((target['project'], target['session'], claimed['close_id']),
                         ('p', self.sid, prepared['close_id']))
        self.assertEqual(self.operator(mid, self.operator_sid)[0], 1)
        self.assertTrue(self.server.call('wait', id=mid, timeout=0.01)['timed_out'])
        finish = ('--finish', claimed['close_id'], '--expect-session', target['session'])
        self.assertEqual(self.close(*finish)[0], 1)
        self.rollout.rename(self.archive)
        self.record_archive(self.archive)
        code, done = self.close(*finish)
        self.assertEqual((code, done['route_removed'], done['operator_receipt']), (0, True, 'done'))
        status = self.server.call('wait', id=mid, timeout=1)
        self.assertEqual((status['timed_out'], status['close_status']), (False, 'done'))

    def test_plain_archive_prefers_exact_native_operator_after_archiving_descendants(self):
        self.enroll(prefer_native=True)
        descendant = self.add_descendant()
        queued = {'delivery_mode': 'queue', 'delivery_path': 'codex-desktop-queue'}
        direct = ('--archive', '--state-dir', self.server.state_dir)
        with mock.patch.object(codex_archive, 'executable', return_value=self.executable), \
                mock.patch.object(codex_preview, 'state_path', return_value=self.db), \
                mock.patch.object(codex_archive.wt, 'open_files', return_value=[]), \
                mock.patch.object(sessions, 'CodexClient') as client, \
                mock.patch.object(codex_live, 'deliver', side_effect=[{'sent': False}, queued]) as deliver:
            archive = client.return_value.__enter__.return_value.call
            archive.side_effect = self.saved_archive
            code, receipt = self.close(*direct)
            self.assertEqual((code, receipt['status'], receipt['closed'], receipt['route_removed'],
                              receipt['operator_delivery_path'], receipt['archive_incomplete']),
                             (1, 'awaiting_native_archive', False, False, 'codex-desktop-queue', True))
            archive.assert_called_once_with('thread/archive', {'threadId': descendant})
            self.assertEqual(self.archive_flags(), {self.sid: 0, descendant: 1})
            self.assertEqual([row['status'] for row in receipt['results']], ['archived', 'awaiting_native_archive'])
            self.assertEqual([call.args[0]['session'] for call in deliver.call_args_list], [self.operator_sid] * 2)
            self.assertEqual([call.kwargs['dry_run'] for call in deliver.call_args_list], [True, False])
            self.assertEqual(self.close(*direct)[1]['operator_message_id'], receipt['operator_message_id'])
            self.assertEqual(deliver.call_count, 2)
            self.assertEqual(len(self.archive_flags()), 2)

        self.server.restart()
        route, = self.server.call('routes', project='p')
        self.assertEqual(route['closing_native_scope'], codex_archive.native_handoff_digest(self.db, (descendant,)))
        self.assertEqual(json.loads((Path(self.server.state_dir) / 'routes.json').read_text())['version'], 3)
        mid = receipt['operator_message_id']
        new_child = self.add_descendant()
        self.assertIn('unarchived descendant', self.operator(mid, self.operator_sid)[1]['error'])
        self.assertEqual(self.server.call('show', id=mid)['state'], 'submitted')
        self.remove_descendant(new_child)
        with mock.patch.object(codex_archive.wt, 'open_files', return_value=[]):
            self.assertEqual(self.operator(mid, self.operator_sid)[0], 0)
        self.rollout.rename(self.archive)
        self.record_archive(self.archive)
        finish = ('--finish', receipt['close_id'], '--expect-session', self.sid)
        cascaded_child = self.add_descendant()
        self.saved_archive('thread/archive', {'threadId': cascaded_child})
        code, refused = self.close(*finish)
        self.assertEqual(code, 1)
        self.assertIn('subtree or saved profile changed', refused['error'])
        route, = self.server.call('routes', project='p')
        self.assertEqual(route['closing'], receipt['close_id'])
        self.remove_descendant(cascaded_child)
        code, done = self.close(*finish)
        self.assertEqual((code, done['route_removed'], done['operator_receipt']), (0, True, 'done'))

    def test_native_operator_preflight_refusal_leaves_root_and_descendants_untouched(self):
        self.enroll(prefer_native=True)
        descendant = self.add_descendant()
        error = codex_live.OwnerUnavailable(codex_live.OwnerUnavailable.NO_OWNER)
        with mock.patch.object(codex_archive, 'executable', return_value=self.executable), \
                mock.patch.object(codex_preview, 'state_path', return_value=self.db), \
                mock.patch.object(codex_archive.wt, 'open_files', return_value=[]), \
                mock.patch.object(sessions, 'CodexClient') as client, \
                mock.patch.object(codex_live, 'deliver', side_effect=error) as deliver:
            archive = client.return_value.__enter__.return_value.call
            archive.side_effect = self.saved_archive
            code, refused = self.close('--archive', '--state-dir', self.server.state_dir)
            self.assertEqual(code, 1)
            self.assertIn('Enrolled Codex close operator is unavailable', refused['error'])
            self.assertTrue(deliver.call_args.kwargs['dry_run'])
            self.assertEqual(self.archive_flags(), {self.sid: 0, descendant: 0})
            archive.assert_not_called()
            route, = self.server.call('routes', project='p')
            self.assertNotIn('closing', route)
            self.assertEqual(self.server.call('ping')['messages'], 0)

            code, confirmed = self.close('--archive', '--keep-route', '--state-dir', self.server.state_dir)
            self.assertEqual((code, confirmed['archived'], confirmed['route_removed']), (0, True, False))
            self.assertEqual(self.archive_flags(), {self.sid: 1, descendant: 1})
            self.assertEqual([call.args[1]['threadId'] for call in archive.call_args_list], [descendant, self.sid])
            deliver.assert_called_once()

    def test_native_admissibility_refuses_unfinished_correspondence_before_leaf_archive(self):
        self.enroll(prefer_native=True)
        descendant = self.add_descendant()
        self.server.call('register', project='p', name='peer', cwd=str(self.cwd))
        pending = self.server.call('send', project='p', sender='peer', recipient='target', body='work', key='fixture')
        with mock.patch.object(codex_archive, 'executable', return_value=self.executable), \
                mock.patch.object(codex_preview, 'state_path', return_value=self.db), \
                mock.patch.object(codex_archive.wt, 'open_files', return_value=[]), \
                mock.patch.object(sessions, 'CodexClient') as client, \
                mock.patch.object(codex_live, 'deliver') as deliver:
            code, refused = self.close('--archive', '--state-dir', self.server.state_dir)
        self.assertEqual(code, 1)
        self.assertIn('unfinished correspondence', refused['error'])
        self.assertEqual(self.archive_flags(), {self.sid: 0, descendant: 0})
        self.assertEqual(self.server.call('show', id=pending['id'])['state'], 'pending')
        client.assert_not_called()
        deliver.assert_not_called()

    def test_existing_operator_without_preference_respects_direct_archive_policy(self):
        self.enroll()
        Path(self.server.state_dir, 'policy.json').write_text(
            json.dumps({'codex_self_archive': False}), encoding='utf-8')
        self.assertEqual(self.server.call('close-operator-status'), {'operator': None})
        with mock.patch.object(codex_archive, 'executable', return_value=self.executable), \
                mock.patch.object(codex_preview, 'state_path', return_value=self.db), \
                mock.patch.object(codex_archive.wt, 'open_files', return_value=[]), \
                mock.patch.object(sessions, 'CodexClient') as client, \
                mock.patch.object(codex_live, 'deliver') as deliver:
            archive = client.return_value.__enter__.return_value.call
            archive.side_effect = self.saved_archive
            code, result = self.close('--archive', '--state-dir', self.server.state_dir)
            self.assertEqual((code, result['route_removed'], result['archived']), (0, True, True))
            archive.assert_called_once_with('thread/archive', {'threadId': self.sid})
            deliver.assert_not_called()

    def test_plain_close_detaches_without_scheduling_enrolled_operator(self):
        self.enroll()
        with mock.patch.object(codex_live, 'deliver') as deliver:
            code, detached = self.close()
            deliver.assert_not_called()
        self.assertEqual((code, detached['method'], detached['archived'], detached['route_removed']),
                         (0, 'detach', False, True))
        self.assertEqual(self.server.call('ping')['messages'], 0)
        self.assertEqual(self.server.call('routes', project='p'), [])
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.rollout.read_text(encoding='utf-8'), 'test transcript')
        with closing(sqlite3.connect(self.db)) as db:
            row = db.execute('SELECT archived, rollout_path FROM threads WHERE id=?', (self.sid,)).fetchone()
        self.assertEqual(row, (0, str(self.rollout)))

    def test_ambiguous_delivery_keeps_route_and_is_never_resent(self):
        self.enroll()
        with mock.patch.object(codex_live, 'deliver',
                               side_effect=dispatch_raises(codex_live.DeliveryError('unknown', True))) as deliver:
            code, prepared = self.close('--archive', '--handoff')
            self.assertEqual((code, prepared['operator_delivery'], prepared['route_removed']),
                             (1, 'uncertain', False))
            self.server.restart()
            self.assertEqual(self.close('--archive', '--handoff')[1]['operator_delivery'], 'uncertain')
            self.assertEqual(dispatches(deliver), 1)
        with self.assertRaisesRegex(ValueError, 'Only pending'):
            self.server.call('claim', id=prepared['operator_message_id'])
        self.assertEqual(self.server.call('routes', project='p')[0]['closing'], prepared['close_id'])

    def test_operator_replacement_refuses_original_request(self):
        self.enroll()
        with mock.patch.object(codex_live, 'deliver', return_value={'delivery_mode': 'idle', 'turn_id': 'test'}) as deliver:
            code, prepared = self.close('--archive', '--handoff')
            self.assertEqual(code, 0)
            current = next(route for route in self.server.call('routes', project='ops')
                           if route['name'] == 'archive')
            self.server.call('unregister', project='ops', name='archive')
            self.server.call('register', project='ops', name='archive', cwd=current['cwd'],
                             transport=current['transport'], executable=current['executable'],
                             session=str(uuid.uuid4()))
            self.assertEqual(self.operator(prepared['operator_message_id'], self.operator_sid)[0], 1)
            self.assertEqual(self.close('--archive', '--handoff')[1]['operator_message_id'], prepared['operator_message_id'])
            self.assertEqual(dispatches(deliver), 1)

    def test_enrolled_operator_cannot_be_its_own_close_target(self):
        self.enroll(self.sid)
        with mock.patch.object(codex_live, 'deliver') as deliver:
            code, error = self.close('--archive', '--handoff')
            self.assertEqual(code, 1)
            self.assertIn('cannot archive itself', error['error'])
            self.assertNotIn('closing', self.server.call('routes', project='p')[0])
            deliver.assert_not_called()


class CloseCommandTests(unittest.TestCase):
    def test_busy_provider_close_exits_nonzero_and_keeps_route(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as cwd, \
                FakeAgyServer(cwd, str(uuid.uuid4())) as fake, \
                mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF}):
            claude_sid = str(uuid.uuid4())
            registry = Path(cwd) / 'claude-session.json'
            registry.write_text(json.dumps({'entrypoint': 'claude-desktop'}), encoding='utf-8')
            claude_session = {'id': claude_sid, 'registry': str(registry), 'pid': 123}
            server.call('register', project='p', name='claude', cwd=cwd, transport='claude-peer', session=claude_sid)
            server.call('register', project='p', name='agy', cwd=cwd, transport='agy', session=fake.sid)
            routes = server.call('routes', project='p')
            for name, reason in (('claude', 'is still running'), ('agy', 'is running')):
                for flags in ([], ['--archive']):
                    with self.subTest(name=name, flags=flags):
                        command = ['--socket', server.path, 'close', name, '--project', 'p',
                                   '--timeout', '0.01', '--no-retro', 'fixture', *flags]
                        with mock.patch.object(sessions, 'resolve_claude_peer', return_value=claude_session), \
                                mock.patch.object(sessions, 'pid_alive', return_value=True), \
                                mock.patch.object(sessions.os, 'kill'):
                            code, result = run(command)
                        self.assertEqual(code, 1)
                        self.assertIn(reason, result['error'])
                        self.assertEqual(server.call('routes', project='p'), routes)

    def test_desktop_archive_the_app_overwrote_exits_nonzero_and_keeps_the_route(self):
        real_archive = sessions.archive_claude_desktop
        with RunningBroker() as server, tempfile.TemporaryDirectory() as cwd:
            sid, host = str(uuid.uuid4()), 'local_' + str(uuid.uuid4())
            registry = Path(cwd) / 'claude-session.json'
            registry.write_text(json.dumps({'entrypoint': 'claude-desktop', 'hostSessionId': host}), encoding='utf-8')
            desktop = Path(cwd) / 'desktop' / 'acct' / 'org'
            desktop.mkdir(parents=True)
            meta = desktop / f'{host}.json'
            original = json.dumps({'sessionId': host, 'cliSessionId': sid, 'isArchived': False})
            meta.write_text(original, encoding='utf-8')
            clock = FakeClock(lambda clock: meta.write_text(original, encoding='utf-8'))
            def archive(*args, **kwargs):
                return real_archive(*args, **{**kwargs, 'clock': clock, 'sleep': clock.sleep})
            server.call('register', project='p', name='claude', cwd=cwd, transport='claude-peer', session=sid)
            routes = server.call('routes', project='p')
            with mock.patch.dict(os.environ, {'CLAUDE_DESKTOP_SESSIONS_DIR': str(Path(cwd) / 'desktop')}), \
                    mock.patch.object(sessions, 'resolve_claude_peer',
                                      return_value={'id': sid, 'registry': str(registry), 'pid': 123}), \
                    mock.patch.object(sessions, 'pid_alive', return_value=False), \
                    mock.patch.object(sessions.os, 'kill'), \
                    mock.patch.object(sessions, 'archive_claude_desktop', side_effect=archive):
                code, result = run(['--socket', server.path, 'close', 'claude', '--project', 'p',
                                    '--no-retro', 'fixture', '--archive'])
            self.assertEqual(code, 1)
            self.assertEqual((result['archived'], result['status'], result['route_removed']),
                             (False, 'archive_unconfirmed', False))
            self.assertEqual(result['cleanup_error'], 'Provider archival was not confirmed')
            self.assertEqual(server.call('routes', project='p'), routes)

    def test_a_failed_close_writes_no_retro_and_a_confirmed_one_writes_it_once(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as cwd:
            store = Path(cwd) / 'retros.jsonl'
            sid = str(uuid.uuid4())
            server.call('register', project='p', name='n', cwd=cwd, transport='codex', session=sid)
            command = ['--socket', server.path, 'close', 'n', '--project', 'p', '--archive', '--store', str(store),
                       '--no-retro', 'nothing to report']
            refused = {'closed': False, 'method': 'archive', 'archived': False, 'session': sid, 'host': 'codex'}
            archived = {**refused, 'closed': True, 'archived': True}

            def records():
                return store.read_text().splitlines() if store.exists() else []

            # A refused archive, twice, then a provider error: the route stays and nothing is recorded.
            with mock.patch.object(bridge, 'close_session', side_effect=[refused, refused, OSError('provider gone')]):
                for _ in range(2):
                    code, out = run(command)
                    self.assertEqual((code, out['archived'], out['route_removed'], out['retro_recorded']),
                                     (1, False, False, False))
                    self.assertNotIn('retro_id', out)
                code, out = run(command)
                self.assertEqual(code, 1)
            self.assertEqual(records(), [])
            self.assertEqual(len(server.call('routes', project='p')), 1)

            # The requirement is still checked before the provider is touched.
            with mock.patch.object(bridge, 'close_session') as close:
                code, out = run(command[:-2])
                self.assertEqual((code, close.called), (1, False))
                self.assertIn('RETRO_REQUIRED', out['error'])

            with mock.patch.object(bridge, 'close_session', return_value=archived):
                code, out = run(command)
            self.assertEqual((code, out['archived'], out['route_removed']), (0, True, True))
            self.assertNotIn('retro_recorded', out)
            self.assertEqual([json.loads(line)['id'] for line in records()], [out['retro_id']])

            # A handoff records before it marks the route closing: a closing route cannot send the notification.
            server.call('register', project='p', name='n', cwd=cwd, transport='codex', session=sid)
            order = []
            with mock.patch.object(bridge, 'record_retro', side_effect=lambda *_: order.append('retro') or {'retro_id': 'r'}), \
                    mock.patch.object(bridge, 'prepare_archive_handoff',
                                      side_effect=lambda *_: order.append('handoff') or {'status': 'awaiting_native_archive'}):
                code, out = run(command + ['--handoff'])
            self.assertEqual((order, out['retro_id'], out['status']), (['retro', 'handoff'], 'r', 'awaiting_native_archive'))
            server.call('unregister', project='p', name='n')

            # A session that closed is not reopened because its retro could not be saved: the gap is named.
            server.call('register', project='p', name='n', cwd=cwd, transport='codex', session=sid)
            with mock.patch.object(bridge, 'close_session', return_value=archived), \
                    mock.patch.object(bridge.retros, 'append', side_effect=OSError('disk full')):
                code, out = run(command)
            self.assertEqual((out['archived'], out['route_removed'], out['retro_recorded'], out['retro_error']),
                             (True, True, False, 'disk full'))
            self.assertEqual(len(records()), 1)

    def test_codex_and_agy_detach_by_default_and_archive_when_requested(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as cwd, \
                FakeAgyServer(cwd, str(uuid.uuid4())) as fake, \
                mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF}):
            codex_sid = str(uuid.uuid4())
            server.call('register', project='p', name='n', cwd=cwd, transport='codex', session=codex_sid)
            code, out = run(['--socket', server.path, 'close', 'n', '--project', 'p', '--no-retro', 'provider behavior fixture'])
            self.assertEqual(
                (code, out['closed'], out['method'], out['archived'], out['resumable'], out['route_removed']),
                (0, True, 'detach', False, True, True),
            )
            self.assertEqual(server.call('routes', project='p'), [])

            server.call('register', project='p', name='j', cwd=cwd, transport='agy', session=fake.other_sid)
            code_j, out_j = run(['--socket', server.path, 'close', 'j', '--project', 'p', '--no-retro', 'provider behavior fixture'])
            self.assertEqual(
                (code_j, out_j['closed'], out_j['method'], out_j['archived'], out_j['host'], out_j['resumable'], out_j['route_removed']),
                (0, True, 'detach', False, 'agy', True, True),
            )
            self.assertEqual(server.call('routes', project='p'), [])
            sent = [b for p, _, b in fake.requests if p.endswith('/UpdateConversationAnnotations')]
            self.assertEqual(sent, [])

            # Passing --archive archives both Antigravity and Codex sessions.
            server.call('register', project='p', name='j-arch', cwd=cwd, transport='agy', session=fake.other_sid)
            code_ja, out_ja = run(['--socket', server.path, 'close', 'j-arch', '--project', 'p', '--archive', '--no-retro', 'provider behavior fixture'])
            self.assertEqual(
                (code_ja, out_ja['closed'], out_ja['method'], out_ja['archived'], out_ja['route_removed']),
                (0, True, 'archive', True, True),
            )
            sent_arch = [b for p, _, b in fake.requests if p.endswith('/UpdateConversationAnnotations')]
            self.assertEqual(sent_arch, [{'cascadeId': fake.other_sid, 'annotations': {'archived': True}, 'mergeAnnotations': True}])

            state_db = Path(cwd) / 'state_5.sqlite'
            os.chmod(cwd, 0o700)
            rollout = Path(cwd) / 'sessions' / f'{codex_sid}.jsonl'
            archived_rollout = Path(cwd) / 'archived_sessions' / rollout.name
            rollout.parent.mkdir()
            archived_rollout.parent.mkdir()
            rollout.write_text('transcript', encoding='utf-8')
            with closing(sqlite3.connect(state_db)) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER NOT NULL DEFAULT 0, archived_at INTEGER, title TEXT, updated_at INTEGER, rollout_path TEXT)")
                conn.execute("INSERT INTO threads VALUES (?, ?, 0, NULL, 'fixture', 1, ?)", (codex_sid, cwd, str(rollout)))
                conn.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)')
            os.chmod(state_db, 0o600)
            server.call('register', project='p', name='n-arch', cwd=cwd, transport='codex', session=codex_sid)
            Path(server.state_dir, 'policy.json').write_text(
                json.dumps({'codex_self_archive': False}), encoding='utf-8')
            def archive(method, params):
                self.assertEqual((method, params), ('thread/archive', {'threadId': codex_sid}))
                rollout.rename(archived_rollout)  # Codex moves the rollout when it archives natively.
                with closing(sqlite3.connect(state_db)) as conn, conn:
                    conn.execute('UPDATE threads SET archived=1, archived_at=2, rollout_path=? WHERE id=?',
                                 (str(archived_rollout), codex_sid))
                return {}

            with mock.patch.object(codex_preview, 'state_path', return_value=state_db), \
                    mock.patch.dict(os.environ, {'CODEX_HOME': cwd}), \
                    mock.patch.object(codex_archive, 'executable', return_value='/fixture/codex'), \
                    mock.patch.object(codex_archive.wt, 'open_files', return_value=[]), \
                    mock.patch.object(sessions, 'CodexClient') as native:
                native.return_value.__enter__.return_value.call.side_effect = archive
                code_na, out_na = run(['--socket', server.path, 'close', 'n-arch', '--project', 'p', '--archive', '--state-dir', server.state_dir,
                                       '--no-retro', 'provider behavior fixture'])
            self.assertEqual(
                (code_na, out_na['closed'], out_na['method'], out_na['archived'], out_na['route_removed']),
                (0, True, 'archive', True, True),
            )
            with closing(sqlite3.connect(state_db)) as conn:
                row = conn.execute('SELECT archived, archived_at FROM threads WHERE id=?', (codex_sid,)).fetchone()
            self.assertEqual(row[0], 1)
            self.assertIsNotNone(row[1])

    def test_realtime_route_is_removed_and_old_broker_is_reported(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as cwd:
            server.call('register', project='p', name='w', cwd=cwd)
            server.call('register', project='p', name='k', cwd=cwd)
            code, out = run(['--socket', server.path, 'close', 'w', '--project', 'p', '--no-retro', 'provider behavior fixture'])
            self.assertEqual((code, out['closed'], out['route_removed']), (0, False, True))
            code, out = run(['--socket', server.path, 'close', 'k', '--project', 'p', '--keep-route', '--no-retro', 'provider behavior fixture'])
            self.assertEqual((code, out['route_removed']), (0, False))
            real = bridge.broker.request
            def old(path, args, timeout=10):
                if args['op'] == 'unregister':
                    raise ValueError('Unknown operation')
                return real(path, args, timeout)
            with mock.patch.object(bridge.broker, 'request', side_effect=old):
                code, out = run(['--socket', server.path, 'close', 'k', '--project', 'p', '--no-retro', 'provider behavior fixture'])
            self.assertEqual((code, out['route_removed']), (0, False))
            self.assertIn('restart it', out['route_note'])
            code, out = run(['--socket', server.path, 'close', 'gone', '--project', 'p', '--no-retro', 'provider behavior fixture'])
            self.assertEqual(code, 1)

    def test_close_agy_with_two_live_servers_and_third_folder_picks_sessions_server(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as base:
            root = Path(base).resolve()
            win_a = root / 'win-a'
            win_b = root / 'win-b'
            lane_c = root / 'lane-c'
            win_a.mkdir()
            win_b.mkdir()
            lane_c.mkdir()
            retro_file = root / 'retro.txt'
            retro_file.write_text('Completed lane task cleanly.\n', encoding='utf-8')
            store = root / 'retros.jsonl'
            runner_sid = str(uuid.uuid4())

            with FakeAgyServer(win_a, str(uuid.uuid4())) as srv_a, \
                    FakeAgyServer(win_b, str(uuid.uuid4())) as srv_b:
                summary = {
                    'summary': 'Lane Runner',
                    'stepCount': 12,
                    'status': 'CASCADE_RUN_STATUS_IDLE',
                    'workspaces': [{'workspaceFolderAbsoluteUri': lane_c.as_uri()}],
                    'lastModifiedTime': '2026-11-24T01:00:00Z',
                }
                srv_a.trajectories[runner_sid] = json.loads(json.dumps(summary))
                srv_b.trajectories[runner_sid] = json.loads(json.dumps(summary))
                servers = [
                    {
                        'pid': 4101, 'ppid': 4000, 'orphaned': False, 'app_dir': 'antigravity',
                        'host': 'ide', 'add_dirs': (str(win_a),), 'url': srv_a.url,
                        'csrf': CSRF, 'summaries': srv_a.trajectories,
                    },
                    {
                        'pid': 4102, 'ppid': 4000, 'orphaned': False, 'app_dir': 'antigravity',
                        'host': 'ide', 'add_dirs': (str(win_b),), 'url': srv_b.url,
                        'csrf': CSRF, 'summaries': srv_b.trajectories,
                    },
                ]
                server.call('register', project='p', name='runner', cwd=str(lane_c),
                            transport='agy', session=runner_sid)
                with mock.patch.object(agy_live, 'discover_servers', return_value=servers):
                    code_s, listed = run(['--socket', server.path, 'sessions', '--provider', 'agy', '--with-model'])
                    self.assertEqual(code_s, 0)
                    row = next(r for r in listed['providers']['agy']['sessions'] if r['id'] == runner_sid)
                    self.assertEqual(row['pid'], 4101)
                    self.assertNotIn('matched multiple live servers', row['model'].get('reason', ''))

                    code_c, closed = run([
                        '--socket', server.path, 'close', 'runner', '--project', 'p',
                        '--archive', '--retro-file', str(retro_file), '--store', str(store),
                        '--card', 'repo#144', '--urgency', 'note', '--tags', 'agy',
                    ])
                self.assertEqual((code_c, closed['closed'], closed['archived'], closed['route_removed']),
                                 (0, True, True, True))
                self.assertIn('retro_id', closed)
                self.assertEqual(server.call('routes', project='p'), [])
                sent_a = [b for p, _, b in srv_a.requests if p.endswith('/UpdateConversationAnnotations')]
                sent_b = [b for p, _, b in srv_b.requests if p.endswith('/UpdateConversationAnnotations')]
                self.assertEqual(sent_a, [{'cascadeId': runner_sid, 'annotations': {'archived': True}, 'mergeAnnotations': True}])
                self.assertEqual(sent_b, [])

    def test_close_archive_by_provider_and_session_without_route_or_folder(self):
        with RunningBroker() as server, tempfile.TemporaryDirectory() as base:
            root = Path(base).resolve()
            gone_wt = root / '.worktrees' / 'repo' / 'jinni-150'
            gone_wt.mkdir(parents=True)
            orphan_sid = str(uuid.uuid4())
            unused_sock = root / 'unused.sock'

            with FakeAgyServer(root, str(uuid.uuid4())) as fake, \
                    mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF}):
                fake.trajectories[orphan_sid] = {
                    'summary': 'Landed Runner With Deleted Worktree',
                    'stepCount': 30,
                    'status': 'CASCADE_RUN_STATUS_IDLE',
                    'workspaces': [{'workspaceFolderAbsoluteUri': gone_wt.as_uri()}],
                    'lastModifiedTime': '2026-11-24T01:20:00Z',
                }

                # Validation refusals on route-less close.
                for argv, expected in (
                    (['close', '--session', orphan_sid, '--provider', 'agy'],
                     'Route-less close --provider and --session requires --archive'),
                    (['close', '--archive', '--session', orphan_sid, '--provider', 'codex'],
                     "Route-less close --session is supported only for --provider agy, not 'codex'"),
                    (['close', '--archive', '--session', orphan_sid],
                     'Use close NAME --project P or close --archive --provider agy --session UUID'),
                    (['close', 'runner', '--project', 'p', '--archive', '--session', orphan_sid, '--provider', 'agy'],
                     'Use close NAME --project P or close --archive --provider agy --session UUID, not both'),
                    (['close', '--archive', '--session', orphan_sid, '--provider', 'agy', '--keep-route'],
                     '--finish, --handoff, --keep-route and --expect-session require a registered route NAME --project P'),
                    (['close', '--archive', '--session', orphan_sid, '--provider', 'agy', '--retro-file', '/tmp/x'],
                     '--retro-file requires a registered route NAME --project P; use retro add for standalone records'),
                    (['close', '--archive', '--session', orphan_sid, '--provider', 'agy', '--no-retro', '   '],
                     'RETRO_REASON_EMPTY'),
                ):
                    with self.subTest(argv=argv):
                        code, err = run(['--socket', str(unused_sock), *argv])
                        self.assertEqual(code, 1)
                        self.assertIn(expected, err['error'])

                def archive_calls():
                    return [b for p, _, b in fake.requests if p.endswith('/UpdateConversationAnnotations')]

                # 1) Broker unreachable -> refused before any provider call.
                code_unreach, err_unreach = run([
                    '--socket', str(unused_sock), 'close', '--archive',
                    '--session', orphan_sid, '--provider', 'agy', '--state-dir', server.state_dir,
                ])
                self.assertEqual(code_unreach, 1)
                self.assertIn('Agent Bridge broker is unavailable', err_unreach['error'])
                self.assertEqual(archive_calls(), [])

                # 2) Route bound in another project -> refused naming the route and project, no archive call.
                server.call('register', project='other-proj', name='worker-150',
                            cwd=str(gone_wt), transport='agy', session=orphan_sid)
                code_bound, err_bound = run([
                    '--socket', server.path, 'close', '--archive',
                    '--session', orphan_sid, '--provider', 'agy', '--state-dir', server.state_dir,
                ])
                self.assertEqual(code_bound, 1)
                self.assertIn("registered as route 'worker-150' in project 'other-proj'", err_bound['error'])
                self.assertIn('use close worker-150 --project other-proj --archive', err_bound['error'])
                self.assertEqual(archive_calls(), [])

                # Unregister the route and delete the worktree folder.
                server.call('unregister', project='other-proj', name='worker-150')
                gone_wt.rmdir()
                self.assertFalse(gone_wt.exists())

                # 3) Unreadable, missing, or corrupt routing snapshot -> refused, no archive call.
                missing_state = root / 'missing.state'
                code_missing, err_missing = run([
                    '--socket', server.path, 'close', '--archive',
                    '--session', orphan_sid, '--provider', 'agy', '--state-dir', str(missing_state),
                ])
                self.assertEqual(code_missing, 1)
                self.assertIn('Routing snapshot could not be read', err_missing['error'])
                self.assertEqual(archive_calls(), [])

                bad_state = root / 'bad.state'
                bad_state.mkdir(mode=0o700)
                bad_routes = bad_state / 'routes.json'
                bad_routes.write_text('{corrupt-json', encoding='utf-8')
                bad_routes.chmod(0o600)
                code_corrupt, err_corrupt = run([
                    '--socket', server.path, 'close', '--archive',
                    '--session', orphan_sid, '--provider', 'agy', '--state-dir', str(bad_state),
                ])
                self.assertEqual(code_corrupt, 1)
                self.assertIn('Routing snapshot could not be read', err_corrupt['error'])
                self.assertEqual(archive_calls(), [])

                bad_routes.chmod(0o644)
                code_perm, err_perm = run([
                    '--socket', server.path, 'close', '--archive',
                    '--session', orphan_sid, '--provider', 'agy', '--state-dir', str(bad_state),
                ])
                self.assertEqual(code_perm, 1)
                self.assertIn('Routing snapshot could not be read', err_perm['error'])
                self.assertEqual(archive_calls(), [])

                # 4) No route + reachable broker + deleted folder -> archives cleanly.
                code, out = run(['--socket', server.path, 'close', '--archive',
                                 '--session', orphan_sid, '--provider', 'agy', '--state-dir', server.state_dir])
                self.assertEqual(
                    (code, out['provider'], out['transport'], out['closed'], out['archived'], out['route_removed']),
                    (0, 'agy', 'agy', True, True, False),
                )
                self.assertEqual(out['session'], orphan_sid)
                self.assertEqual(len(archive_calls()), 1)
                _, listed = run(['--socket', server.path, 'sessions', '--provider', 'agy'])
                self.assertNotIn(orphan_sid, {r['id'] for r in listed['providers']['agy']['sessions']})

                # ArchiveUncertain without a route reports provider and session instead of name=None.
                fake.ignore_archive = True
                fake.trajectories[orphan_sid]['annotations'] = {}
                code_u, err_u = run(['--socket', server.path, 'close', '--archive',
                                     '--session', orphan_sid, '--provider', 'agy', '--state-dir', server.state_dir])
                self.assertEqual((code_u, err_u['uncertain'], err_u['provider'], err_u['session']),
                                 (1, True, 'agy', orphan_sid))
                self.assertNotIn('name', err_u)


if __name__ == '__main__':
    unittest.main()
