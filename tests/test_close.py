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

import agy_live
import bridge
import codex_live
import codex_archive
import codex_preview
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
        )
        self.assertEqual(
            (archived['closed'], archived['method'], archived['archived'], archived['host']),
            (True, 'archive', True, 'claude-desktop'),
        )
        self.assertIs(json.loads(meta_file.read_text(encoding='utf-8'))['isArchived'], True)

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
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER, rollout_path TEXT)')
            db.execute('INSERT INTO threads VALUES (?, ?, 0, ?)', (self.sid, str(self.cwd), str(self.rollout)))
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

    def enroll(self, session=None):
        self.operator_sid = session or str(uuid.uuid4())
        self.server.call('register', project='ops', name='archive', cwd=str(self.cwd), transport='codex',
                         session=self.operator_sid, executable=self.executable)
        self.server.call('register', project='ops', name='trigger', cwd=str(self.cwd))
        config = Path(self.server.state_dir) / 'close-operator.json'
        config.write_text(json.dumps({'project': 'ops', 'name': 'archive', 'session': self.operator_sid,
                                      'sender': 'trigger'}), encoding='utf-8')
        config.chmod(0o600)

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
        self.assertIn('active delivery', self.close('--archive', '--handoff')[1]['error'])
        self.server.call('finish', id=pending['id'], state='submitted')
        self.server.call('progress', id=pending['id'], note='working')
        self.assertIn('unfinished correspondence', self.close('--archive', '--handoff')[1]['error'])
        self.server.call('resolve', id=pending['id'], state='done', note='reviewed')
        self.assertEqual(self.close('--archive', '--handoff')[0], 0)

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
            deliver.assert_called_once()
        with self.assertRaisesRegex(ValueError, 'reserved'):
            self.server.call('send', project='ops', sender='trigger', recipient='archive',
                             key='agent-bridge-close:' + prepared['close_id'], body='spoof')
        forged = self.server.call('send', project='ops', sender='trigger', recipient='archive',
                                  key='ordinary', body='copy of archive notification')
        self.assertEqual(self.operator(forged['id'], self.operator_sid)[0], 1)
        self.assertEqual(self.operator(mid, self.sid)[0], 1)
        code, target = self.operator(mid, self.operator_sid)
        self.assertEqual((code, target['project'], target['session'], target['close_id']),
                         (0, 'p', self.sid, prepared['close_id']))
        self.assertEqual(self.operator(mid, self.operator_sid)[0], 1)
        self.assertTrue(self.server.call('wait', id=mid, timeout=0.01)['timed_out'])
        finish = ('--finish', target['close_id'], '--expect-session', target['session'])
        self.assertEqual(self.close(*finish)[0], 1)
        self.rollout.rename(self.archive)
        self.record_archive(self.archive)
        code, done = self.close(*finish)
        self.assertEqual((code, done['route_removed'], done['operator_receipt']), (0, True, 'done'))
        status = self.server.call('wait', id=mid, timeout=1)
        self.assertEqual((status['timed_out'], status['close_status']), (False, 'done'))

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
        with mock.patch.object(codex_live, 'deliver', side_effect=codex_live.DeliveryError('unknown', True)) as deliver:
            code, prepared = self.close('--archive', '--handoff')
            self.assertEqual((code, prepared['operator_delivery'], prepared['route_removed']),
                             (1, 'uncertain', False))
            self.server.restart()
            self.assertEqual(self.close('--archive', '--handoff')[1]['operator_delivery'], 'uncertain')
            deliver.assert_called_once()
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
            deliver.assert_called_once()

    def test_enrolled_operator_cannot_be_its_own_close_target(self):
        self.enroll(self.sid)
        with mock.patch.object(codex_live, 'deliver') as deliver:
            code, error = self.close('--archive', '--handoff')
            self.assertEqual(code, 1)
            self.assertIn('cannot archive itself', error['error'])
            self.assertNotIn('closing', self.server.call('routes', project='p')[0])
            deliver.assert_not_called()


class CloseCommandTests(unittest.TestCase):
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
            with closing(sqlite3.connect(state_db)) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER NOT NULL DEFAULT 0, archived_at INTEGER, title TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, 0, NULL, 'fixture', 1)", (codex_sid, cwd))
                conn.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)')
            os.chmod(state_db, 0o600)
            server.call('register', project='p', name='n-arch', cwd=cwd, transport='codex', session=codex_sid)
            def archive(method, params):
                self.assertEqual((method, params), ('thread/archive', {'threadId': codex_sid}))
                with closing(sqlite3.connect(state_db)) as conn, conn:
                    conn.execute('UPDATE threads SET archived=1, archived_at=2 WHERE id=?', (codex_sid,))
                return {}

            with mock.patch.object(codex_preview, 'state_path', return_value=state_db), \
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


if __name__ == '__main__':
    unittest.main()
