import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid

import bridge
import broker
import codex_archive
import codex_preview
import codex_queue
import retros
import route_store
from support import RunningBroker
from test_close import run


class QueuedSelfArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.cwd = root / 'workspace'
        self.cwd.mkdir()
        self.sid = str(uuid.uuid4())
        env = mock.patch.dict(os.environ, {'CODEX_HOME': str(root), 'CODEX_SQLITE_HOME': str(root)})
        env.start()
        self.addCleanup(env.stop)
        self.rollout = root / 'sessions' / f'rollout-{self.sid}.jsonl'
        self.archived_rollout = root / 'archived_sessions' / self.rollout.name
        self.rollout.parent.mkdir()
        self.archived_rollout.parent.mkdir()
        self.rollout.write_text('{}\n', encoding='utf-8')
        self.db = root / 'state_5.sqlite'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER, archived_at INTEGER, '
                       'title TEXT, updated_at INTEGER, rollout_path TEXT)')
            db.execute("INSERT INTO threads VALUES (?, ?, 0, NULL, 'fixture', 1, ?)",
                       (self.sid, str(self.cwd), str(self.rollout)))
            db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)')
        self.db.chmod(0o600)
        self.store = route_store.RouteStore(root / 'routing')
        self.addCleanup(self.store.close)
        self.state = broker.State(store=self.store)
        self.address = {'project': 'p', 'name': 'target'}
        self.route = self.state.action({'op': 'register', **self.address, 'transport': 'codex',
                                        'cwd': str(self.cwd), 'session': self.sid})
        self.patch(codex_archive, 'sweep_store', return_value=self.db)
        self.patch(codex_preview, 'state_path', return_value=self.db)
        self.patch(codex_archive, 'process_cwds', return_value=(set(), []))
        self.available = self.patch(codex_queue, 'check_available')
        self.enqueue = self.patch(codex_queue, 'enqueue')
        self.args = {**self.address, 'expected_route': self.route, 'timeout': 1,
                     'native_scope': codex_archive.native_handoff_scope(self.route)[1]}
        self.state.action({'op': 'register', 'project': 'p', 'name': 'caller', 'cwd': str(self.cwd)})
        self.mid = self.state.action({'op': 'send', 'project': 'p', 'sender': 'caller',
                                      'recipient': 'target', 'body': 'fixture', 'key': 'fixture'})['id']
        self.state.action({'op': 'resolve', 'id': self.mid, 'state': 'done', 'note': 'fixture complete'})

    def patch(self, target, field, **kwargs):
        patcher = mock.patch.object(target, field, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def record_archive_bit(self, rollout):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE threads SET archived=1, rollout_path=? WHERE id=?', (str(rollout), self.sid))

    def record_native_archive(self):
        self.rollout.rename(self.archived_rollout)
        self.record_archive_bit(self.archived_rollout)

    def archive_events(self):
        return [(event['archive_path'], event['note']) for event in self.state.events
                if event['type'] == 'close_archive']

    def finish_args(self, result):
        return {**self.address, 'close_id': result['closing'], 'session': self.sid, 'timeout': 1}

    def prepare_stop_retro(self):
        row = retros.record('p', None, 'target', None, self.sid, reason='fixture', urgency='stop')
        store = self.cwd.parent / 'retros.jsonl'
        return {'store': str(store), 'row': row, 'target': 'caller'}

    def assert_closed(self, result, path, note):
        self.assertTrue(result['route_removed'])
        self.assertEqual(result['archive_path'], path)
        self.assertEqual(result['note'], note)
        self.assertEqual(self.archive_events()[-1], (path, note))
        self.assertNotIn(('p', 'target'), self.state.endpoints)
        self.assertNotIn(self.mid, self.state.messages)
        self.assertEqual(result['correspondence_cleanup']['removed'], 1)
        codex_preview.verify_archive(self.route)

    def assert_refused_keeps_open_route(self, result, reason):
        self.assertEqual(result['archive_path'], 'not_archived')
        self.assertFalse(result['route_removed'])
        self.assertEqual(result['reason'], reason)
        self.assertEqual(self.archive_events(), [('not_archived', reason)])
        self.assertEqual(self.state.route('p', 'target'), self.route)
        self.assertEqual(broker.State(store=self.store).route('p', 'target'), self.route)
        self.assertEqual(self.state.messages[self.mid]['state'], 'done')

    async def test_exact_entry_archives_and_releases_only_after_readback(self):
        def submit(thread, prompt, timeout):
            self.assertEqual(thread, self.sid)
            self.assertEqual(prompt, 'Archive this Codex Desktop thread now. Call the Codex app '
                             f'set_thread_archived tool with threadId "{self.sid}", hostId "local", '
                             'source "codex", and archived true. Take no other action.')
            self.assertTrue(self.state.endpoints[('p', 'target')]['closing_self_archive'])
            self.assertEqual(self.state.messages[self.mid]['state'], 'done')
            self.record_native_archive()

        self.enqueue.side_effect = submit
        result = await broker.start_self_archive(self.state, self.args)
        self.assert_closed(result, 'self_archive', 'Queued self-archive verified')
        self.enqueue.assert_called_once()

    async def test_definite_submit_refusal_reopens_route_without_touching_codex(self):
        self.enqueue.side_effect = codex_queue.QueueUnavailable()
        retro = self.prepare_stop_retro()
        before = self.db.read_bytes()
        result = await broker.start_self_archive(self.state, {**self.args, 'retro': retro})
        self.assert_refused_keeps_open_route(result, 'Desktop queue unavailable; no self-archive entry was submitted')
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(self.rollout.exists())
        self.assertFalse(Path(retro['store']).exists())
        self.assertEqual(list((self.store.directory / 'pending-close-retros').iterdir()), [])
        self.enqueue.assert_called_once()

    async def test_existing_archive_bit_without_native_rollout_is_refused(self):
        self.record_archive_bit(self.rollout)
        self.available.side_effect = broker.ThreadArchived()
        before = self.db.read_bytes()
        result = await broker.start_self_archive(self.state, self.args)
        self.assert_refused_keeps_open_route(result, 'Existing archive bit has no verified native archive')
        self.assertEqual(self.db.read_bytes(), before)
        self.enqueue.assert_not_called()

    async def test_existing_native_archive_is_verified_without_queue(self):
        self.record_native_archive()
        self.available.side_effect = broker.ThreadArchived()
        result = await broker.start_self_archive(self.state, self.args)
        self.assert_closed(result, 'self_archive', 'Thread already natively archived; archive verified')
        self.enqueue.assert_not_called()

    async def test_timeout_keeps_durable_guard_and_finish_only_verifies(self):
        retro = self.prepare_stop_retro()
        result = await broker.start_self_archive(self.state, {**self.args, 'timeout': 0.15, 'retro': retro})
        self.assertEqual(result['archive_path'], 'pending_self_archive')
        self.assertFalse(result['route_removed'])
        self.assertEqual(self.archive_events(), [
            ('pending_self_archive', 'Codex self-archive is not yet verified; verify with --finish')])
        self.assertEqual(self.state.messages[self.mid]['state'], 'done')
        self.assertFalse(Path(retro['store']).exists())
        self.assertEqual(set(self.state.messages), {self.mid})
        restored = broker.State(store=self.store)
        self.assertTrue(restored.endpoints[('p', 'target')]['closing_self_archive'])
        self.assertEqual(restored.messages[self.mid]['state'], 'done')
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            await broker.start_self_archive(restored, self.args)
        with self.assertRaisesRegex(ValueError, 'does not use a close operator'):
            restored.dispatch_close(self.finish_args(result))
        with self.assertRaisesRegex(ValueError, 'route is closing'):
            restored.route('p', 'target')
        self.state = restored
        self.record_archive_bit(self.rollout)
        unverified = await broker.finish_close(restored, {**self.finish_args(result), 'timeout': 0.1})
        self.assertEqual(unverified['archive_path'], 'pending_self_archive')
        self.assertFalse(Path(retro['store']).exists())
        self.record_native_archive()
        with mock.patch.object(restored, 'routing_remove', side_effect=OSError('fixture route save failure')):
            incomplete = await broker.finish_close(restored, self.finish_args(result))
        self.assertFalse(incomplete['route_removed'])
        self.assertEqual(retros.rows(Path(retro['store']), 'p'), [retro['row']])
        finished = await broker.finish_close(restored, self.finish_args(result))
        self.assertTrue(finished['route_removed'])
        self.assertNotIn(self.mid, restored.messages)
        self.assertEqual(retros.rows(Path(retro['store']), 'p'), [retro['row']])
        self.assertEqual(set(restored.messages), {finished['notification']['id']})
        self.assertEqual(finished['notification'], incomplete['notification'])
        self.enqueue.assert_called_once()

    async def test_uncertain_acceptance_keeps_guard_without_fallback(self):
        self.enqueue.side_effect = broker.DeliveryError('ambiguous queue receipt', uncertain=True)
        result = await broker.start_self_archive(self.state, self.args)
        self.assertEqual(result['archive_path'], 'pending_self_archive')
        self.assertEqual(self.archive_events(), [
            ('pending_self_archive', 'Queue acceptance is uncertain; verify with --finish')])
        self.assertEqual(self.state.endpoints[('p', 'target')]['closing'], result['closing'])
        self.assertEqual(self.state.messages[self.mid]['state'], 'done')
        self.enqueue.assert_called_once()

    async def test_unfinished_correspondence_still_blocks_reservation(self):
        notice = self.state.action({'op': 'send', 'project': 'p', 'sender': 'target',
                                    'recipient': 'caller', 'body': 'Ordinary unfinished work', 'key': 'ordinary'})
        with self.assertRaisesRegex(ValueError, 'unfinished correspondence'):
            await broker.start_self_archive(self.state, self.args)
        self.assertEqual(self.state.messages[notice['id']]['state'], 'pending')
        self.assertEqual(self.state.endpoints[('p', 'target')], self.route)
        self.assertEqual(self.archive_events(), [])
        self.enqueue.assert_not_called()

    async def test_close_archive_records_and_notifies_stop_retro_once_after_verification(self):
        store = self.cwd.parent / 'retros.jsonl'
        loop = asyncio.get_running_loop()

        def request(path, args, timeout=10):
            async def dispatch():
                if args['op'] == 'close-self-archive':
                    return await broker.start_self_archive(self.state, args)
                if args['op'] == 'close-finish':
                    return await broker.finish_close(self.state, args)
                return await self.state.action_async(args)

            return asyncio.run_coroutine_threadsafe(dispatch(), loop).result(timeout)

        def close_session(route, timeout, **options):
            self.assertTrue(options['prefer_native']())
            return {'status': 'awaiting_native_archive', 'native_scope': self.args['native_scope'],
                    'results': [{'id': self.sid, 'status': 'awaiting_native_archive'}], 'count': 0}

        def enqueue(*_):
            self.assertFalse(store.exists())
            self.assertEqual(set(self.state.messages), {self.mid})
            self.record_native_archive()

        self.enqueue.side_effect = enqueue
        with mock.patch.object(bridge.broker, 'request', side_effect=request), \
                mock.patch.object(bridge, 'close_session', side_effect=close_session):
            code, result = await asyncio.to_thread(
                run, ['--socket', str(self.cwd.parent / 'fixture.sock'), 'close', 'target', '--project', 'p',
                      '--state-dir', str(self.store.directory), '--archive', '--no-retro', 'fixture',
                      '--urgency', 'stop', '--notify', 'caller', '--store', str(store)])
            self.assertEqual((code, result['archive_path'], result['route_removed']), (0, 'self_archive', True))
            retry_code, _ = await asyncio.to_thread(
                run, ['--socket', str(self.cwd.parent / 'fixture.sock'), 'close', 'target', '--project', 'p',
                      '--finish', result['close_id'], '--expect-session', self.sid])
        self.assertEqual(retry_code, 1)
        row, = retros.rows(store, 'p')
        self.assertEqual((row['id'], row['urgency']), (result['retro_id'], 'stop'))
        notice, = self.state.messages.values()
        self.assertEqual((notice['id'], notice['sender'], notice['state']),
                         (result['notification']['id'], 'target', 'pending'))
        self.assertEqual(self.archive_events(), [('self_archive', 'Queued self-archive verified')])
        self.enqueue.assert_called_once()

    async def test_busy_thread_accepts_entry_and_archives_after_active_turn(self):
        turn_finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def complete_turn():
            await turn_finished.wait()
            self.assertIn('closing', self.state.endpoints[('p', 'target')])
            self.assertEqual(self.state.messages[self.mid]['state'], 'done')
            self.assertEqual(self.archive_events(), [])
            self.record_native_archive()

        def submit(thread, prompt, timeout):
            self.assertFalse(turn_finished.is_set())
            loop.call_soon_threadsafe(turn_finished.set)

        self.enqueue.side_effect = submit
        result, _ = await asyncio.gather(broker.start_self_archive(self.state, self.args), complete_turn())
        self.assert_closed(result, 'self_archive', 'Queued self-archive verified')
        self.enqueue.assert_called_once()


class SelfArchiveSelectionTests(unittest.TestCase):
    def test_policy_off_skips_queue_and_uses_direct_native_archive(self):
        with RunningBroker() as server:
            sid = str(uuid.uuid4())
            route = server.call('register', name='target', project='p', transport='codex',
                                session=sid, cwd=server.temp.name)
            Path(server.state_dir, 'policy.json').write_text(json.dumps({'codex_self_archive': False}))

            def close_session(target, timeout, **options):
                self.assertEqual(target, route)
                self.assertFalse(options['prefer_native']())
                return {'closed': True, 'archived': True, 'method': 'archive', 'session': sid}

            with mock.patch.object(bridge, 'close_session', side_effect=close_session), \
                    mock.patch.object(codex_queue, 'check_available', side_effect=codex_queue.QueueUnavailable()) as queue:
                code, result = run(['--socket', server.path, 'close', 'target', '--project', 'p',
                                    '--state-dir', server.state_dir, '--archive', '--no-retro', 'fixture'])
            queue.assert_not_called()
            self.assertEqual((code, result['archived'], result['route_removed']), (0, True, True))
            self.assertEqual(server.call('routes', project='p'), [])
