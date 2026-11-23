import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import plistlib
import shutil
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
import codex_wake_open
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
        self.write_rollout(self.rollout, self.sid)
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
        self.patch(route_store.sessions, 'inspect_session',
                   return_value={'session': {'id': self.sid, 'cwd': str(self.cwd)}})
        self.state = broker.State(store=self.store)
        self.address = {'project': 'p', 'name': 'target'}
        self.route = self.state.action({'op': 'register', **self.address, 'transport': 'codex',
                                        'cwd': str(self.cwd), 'session': self.sid})
        self.patch(codex_archive, 'sweep_store', return_value=self.db)
        self.patch(codex_preview, 'state_path', return_value=self.db)
        self.real_check_available = codex_queue.check_available
        self.real_enqueue = codex_queue.enqueue
        self.available = self.patch(codex_queue, 'check_available')
        self.queue_id = str(uuid.uuid4())
        self.enqueue = self.patch(codex_queue, 'enqueue', return_value=self.queue_id)
        self.app = root / 'Test.app'
        self.plist = self.app / 'Contents' / 'Info.plist'
        self.plist.parent.mkdir(parents=True)
        self.write_app_build(*codex_wake_open.APP_BUILD)
        self.patch(codex_queue, 'APP', new=self.app)
        self.patch(codex_queue, 'verify_signature')
        self.opener = self.patch(codex_wake_open, '_open_wake_url')
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

    def write_rollout(self, path, sid, *events):
        rows = [{'type': 'session_meta', 'payload': {'id': sid}}]
        rows.extend({'type': 'event_msg', 'payload': {'type': event, 'turn_id': turn}} for event, turn in events)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def configure_cascade(self):
        self.queue = self.cwd.parent / 'queue_1.sqlite'
        with closing(sqlite3.connect(self.queue)) as db, db:
            db.execute('CREATE TABLE queued_items (thread_id TEXT NOT NULL, message TEXT)')
        self.patch(codex_queue, 'default_stores', return_value=(self.db, self.queue))
        self.patch(codex_archive, 'executable', return_value='/fixture/codex')
        self.patch(codex_archive.wt, 'open_files', return_value=[])
        self.sleep = mock.Mock()
        original = codex_archive.check_cascade_quiescence
        self.patch(codex_archive, 'check_cascade_quiescence',
                   side_effect=lambda *args, **kwargs: original(*args, **kwargs, sleep=self.sleep))
        self.saved = self.patch(codex_archive.sessions, 'CodexClient').return_value.__enter__.return_value.call

        def held(method, params):
            sid = params['threadId']
            raise codex_archive.sessions.CodexRpcError(
                method, {'code': -32600, 'message': f'thread {sid} already has an active writer'})

        self.held = held
        self.saved.side_effect = held
        loop = asyncio.get_running_loop()

        def request(path, args, timeout=10):
            async def dispatch():
                if args['op'] == 'close-self-archive':
                    return await broker.start_self_archive(self.state, args)
                if args['op'] == 'close-finish':
                    return await broker.finish_close(self.state, args)
                return await self.state.action_async(args)
            return asyncio.run_coroutine_threadsafe(dispatch(), loop).result(timeout)

        self.request = self.patch(bridge.broker, 'request', side_effect=request)

    def add_cascade_child(self, *, parent=None, archived=False, events=None):
        sid = str(uuid.uuid4())
        path = (self.archived_rollout if archived else self.rollout).with_name(f'rollout-{sid}.jsonl')
        if events is None:
            events = [('task_started', '1'), ('task_complete', '1')]
        self.write_rollout(path, sid, *events)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO threads VALUES (?, ?, ?, NULL, 'fixture', 1, ?)",
                       (sid, str(self.cwd), int(archived), str(path)))
            db.execute('INSERT INTO thread_spawn_edges VALUES (?, ?)', (parent or self.sid, sid))
        return sid, path

    def archive_cascade_child(self, child, *, move=True):
        sid, path = child
        if move:
            target = self.archived_rollout.with_name(path.name)
            path.rename(target)
            path = target
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE threads SET archived=1, archived_at=2, rollout_path=? WHERE id=?', (str(path), sid))

    async def cascade_cli(self, *flags, timeout=2):
        return await asyncio.to_thread(run, ['--socket', str(self.cwd.parent / 'fixture.sock'), 'close', 'target',
                                             '--project', 'p', '--state-dir', str(self.store.directory),
                                             '--timeout', str(timeout), *flags])

    async def cascade_close(self, **kwargs):
        return await self.cascade_cli('--archive', '--no-retro', 'fixture', **kwargs)

    async def cascade_finish(self, result, **kwargs):
        return await self.cascade_cli('--finish', result['close_id'], '--expect-session', self.sid, **kwargs)

    def wake_archiving(self, *children):
        def archive(thread, _timeout):
            self.assertEqual(thread, self.sid)
            self.record_native_archive()
            for child in children:
                self.archive_cascade_child(child)
        self.opener.side_effect = archive

    def assert_only_root_entry(self):
        self.enqueue.assert_called_once()
        self.opener.assert_called_once()
        self.assertEqual(self.enqueue.call_args.args[:2],
                         (self.sid, 'Archive this Codex Desktop thread now. Call the Codex app '
                          f'set_thread_archived tool with threadId "{self.sid}", hostId "local", '
                          'source "codex", and archived true. Take no other action.'))
        self.assertEqual(self.opener.call_args.args[0], self.sid)

    async def test_cascade_terminal_child_and_dispatch_removed_queue_row(self):
        self.configure_cascade()
        child = self.add_cascade_child()
        with closing(sqlite3.connect(self.queue)) as db, db:
            db.execute('INSERT INTO queued_items VALUES (?, ?)', (child[0], 'fixture'))
            db.execute('DELETE FROM queued_items WHERE thread_id=?', (child[0],))
        self.wake_archiving(child)
        reads = []
        self.available.side_effect = lambda route: reads.append('availability')
        self.sleep.side_effect = lambda _: reads.append('settle')
        with mock.patch.object(codex_archive, 'check_descendant_queue',
                               wraps=codex_archive.check_descendant_queue) as queue_check:
            code, result = await self.cascade_close()
        self.assertEqual((code, result['archive_path'], result['count'], result['route_removed']),
                         (0, 'root_cascade', 2, True))
        self.assertEqual(result['results'][0]['method'], 'root_cascade')
        self.assertEqual(reads, ['availability', 'settle'])
        self.assertEqual(queue_check.call_args_list, [mock.call(self.queue, [child[0]])] * 2)
        self.saved.assert_called_once_with('thread/archive', {'threadId': child[0]})
        self.sleep.assert_called_once_with(1)
        self.assert_only_root_entry()

    async def test_cascade_mixed_saved_previous_and_multiple_nested_workers(self):
        self.configure_cascade()
        intermediate = self.add_cascade_child()
        worker = self.add_cascade_child(parent=intermediate[0])
        previous = self.add_cascade_child(archived=True)
        second = self.add_cascade_child(events=[('task_started', '1'), ('turn_aborted', '1')])
        saved = self.add_cascade_child()

        def rpc(method, params):
            if params['threadId'] == saved[0]:
                self.archive_cascade_child(saved)
                return {}
            self.held(method, params)

        self.saved.side_effect = rpc
        self.wake_archiving(worker, intermediate, second)
        code, result = await self.cascade_close()
        self.assertEqual((code, result['count']), (0, 6))
        rows = {row['id']: row for row in result['results']}
        self.assertEqual(rows[previous[0]]['status'], 'already_archived')
        self.assertEqual(rows[saved[0]]['status'], 'archived')
        self.assertEqual([rows[child[0]]['method'] for child in (intermediate, worker, second)], ['root_cascade'] * 3)
        self.assertEqual({call.args[1]['threadId'] for call in self.saved.call_args_list},
                         {worker[0], second[0], saved[0]})
        self.sleep.assert_called_once_with(1)
        self.assert_only_root_entry()

    async def test_cascade_nonwriter_failure_anywhere_blocks_the_root(self):
        self.configure_cascade()
        child = self.add_cascade_child()
        rejected = self.add_cascade_child()

        def rpc(method, params):
            if params['threadId'] == rejected[0]:
                raise codex_archive.sessions.CodexRpcError(
                    method, {'code': -32600, 'message': f'thread {child[0]} already has an active writer'})
            self.held(method, params)

        self.saved.side_effect = rpc
        code, result = await self.cascade_close()
        self.assertEqual((code, result['route_removed'], result['count']), (1, False, 0))
        self.assertEqual(self.state.route('p', 'target'), self.route)
        self.enqueue.assert_not_called()
        self.opener.assert_not_called()

    async def test_cascade_partial_root_archive_persists_exact_review_and_finishes_after_restart(self):
        self.configure_cascade()
        child = self.add_cascade_child()
        previous = self.add_cascade_child(archived=True)
        self.wake_archiving()
        code, pending = await self.cascade_close(timeout=1.2)
        self.assertEqual((code, pending['archive_path'], pending['root_archived'], pending['count']),
                         (1, 'pending_self_archive', True, 2))
        self.assertFalse(pending['route_removed'])
        data = json.loads(self.store.path.read_text())
        reservation = next(row for row in data['endpoints'] if row['name'] == 'target')
        self.assertEqual(data['version'], 6)
        self.assertEqual(reservation['closing_reviewed_descendants'], sorted([child[0], previous[0]]))
        self.assertEqual(reservation['closing_cascade_deferred'], [child[0]])
        self.state = broker.State(store=self.store)
        self.archive_cascade_child(child, move=False)
        code, pending = await self.cascade_finish(pending, timeout=.15)
        self.assertEqual((code, pending['count'], pending['route_removed']), (1, 2, False))
        self.assertFalse(pending['wake_requested'])
        self.archive_cascade_child(child)
        self.write_rollout(previous[1], child[0])  # An archived sibling with the wrong session_meta.
        code, pending = await self.cascade_finish(pending, timeout=.15)
        self.assertEqual((code, pending['count'], pending['route_removed']), (1, 2, False))
        self.write_rollout(previous[1], previous[0])
        code, closed = await self.cascade_finish(pending)
        self.assertEqual((code, closed['archive_path'], closed['count'], closed['route_removed']),
                         (0, 'root_cascade', 3, True))
        methods = {row['id']: row['method'] for row in closed['results']}
        self.assertEqual((methods[child[0]], methods[previous[0]]), ('root_cascade', 'saved_or_previously_archived'))
        self.assert_only_root_entry()

    async def test_cascade_already_archived_root_keeps_pending_when_child_is_unmet(self):
        self.configure_cascade()
        child = self.add_cascade_child()
        self.record_native_archive()
        self.available.side_effect = broker.ThreadArchived()
        code, pending = await self.cascade_close()
        self.assertEqual((code, pending['archive_path'], pending['root_archived'], pending['count']),
                         (1, 'pending_self_archive', True, 1))
        self.archive_cascade_child(child)
        code, result = await self.cascade_finish(pending)
        self.assertEqual((code, result['archive_path'], result['count']), (0, 'self_archive', 2))
        self.assertEqual(result['results'][0]['method'], 'deferred_native_readback')
        self.enqueue.assert_not_called()
        self.opener.assert_not_called()

    async def test_cascade_enqueue_checkpoint_failure_keeps_reservation_without_retry(self):
        self.configure_cascade()
        child = self.add_cascade_child()
        save = self.store.save

        def fail_confirmation(endpoints, channels):
            if endpoints[('p', 'target')].get('closing_self_archive_enqueued'):
                raise OSError('fixture checkpoint failure')
            return save(endpoints, channels)

        with mock.patch.object(self.store, 'save', side_effect=fail_confirmation):
            code, pending = await self.cascade_close()
        self.assertEqual((code, pending['archive_path']), (1, 'pending_self_archive'))
        self.state = broker.State(store=self.store)
        self.assertEqual(self.state.endpoints[('p', 'target')]['closing'], pending['close_id'])
        self.record_native_archive()
        self.archive_cascade_child(child)
        code, result = await self.cascade_finish(pending)
        self.assertEqual((code, result['count'], result['route_removed']), (0, 2, True))
        self.enqueue.assert_called_once()
        self.opener.assert_not_called()

    async def test_cascade_held_open_unreadable_unbound_or_without_lifecycle(self):
        self.configure_cascade()
        child, path = self.add_cascade_child()
        for state in ('open', 'unreadable', 'unbound', 'no_lifecycle'):
            with self.subTest(state=state):
                if state == 'open':
                    self.write_rollout(path, child, ('task_started', '1'), ('task_complete', 'another-turn'))
                elif state == 'unreadable':
                    path.unlink()
                elif state == 'unbound':
                    self.write_rollout(path, self.sid, ('task_complete', '1'))
                else:
                    self.write_rollout(path, child)
                code, result = await self.cascade_close()
                expected = 'open' if state == 'open' else 'unknown'
                self.assertEqual((code, result['reason'], result['descendant'], result['descendant_state']),
                                 (1, 'descendant_turn_' + expected, child, expected))
                self.assertEqual(self.state.route('p', 'target'), self.route)
        self.enqueue.assert_not_called()
        self.opener.assert_not_called()

    async def test_cascade_nested_open_ancestor_refuses_without_saved_ancestor_archive(self):
        self.configure_cascade()
        ancestor, _ = self.add_cascade_child(events=[('task_started', '1')])
        worker, _ = self.add_cascade_child(parent=ancestor)
        code, result = await self.cascade_close()
        self.assertEqual((code, result['reason'], result['descendant']), (1, 'descendant_turn_open', ancestor))
        self.saved.assert_called_once_with('thread/archive', {'threadId': worker})
        self.enqueue.assert_not_called()

    async def test_cascade_pending_wal_queue_row_and_unreadable_store_refuse(self):
        self.configure_cascade()
        child, _ = self.add_cascade_child()
        wal_queue = self.queue.with_name('queue-writer')
        with closing(sqlite3.connect(wal_queue)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE queued_items (thread_id TEXT, message TEXT)')
            db.execute('INSERT INTO queued_items VALUES (?, ?)', (child, 'sensitive fixture payload'))
            db.commit()
            shutil.copyfile(wal_queue, self.queue)
            shutil.copyfile(str(wal_queue) + '-wal', str(self.queue) + '-wal')
            before = {p.name: p.read_bytes() for p in (self.queue, Path(str(self.queue) + '-wal'))}
            code, result = await self.cascade_close()
            self.assertEqual((code, result['reason'], result['descendant'], result['descendant_state']),
                             (1, 'descendant_queue_pending', child, 'pending'))
            self.assertNotIn('sensitive fixture payload', json.dumps(result))
            self.assertFalse(Path(str(self.queue) + '-shm').exists())
            self.assertEqual({name: self.queue.with_name(name).read_bytes() for name in before}, before)
        self.queue.unlink()
        code, result = await self.cascade_close()
        self.assertEqual((code, result['reason'], result['descendant'], result['descendant_state']),
                         (1, 'descendant_queue_unknown', child, 'unknown'))
        self.assertNotIn(str(self.queue), json.dumps(result))
        self.sleep.assert_not_called()
        self.enqueue.assert_not_called()

    async def test_cascade_settle_refuses_rollover_and_late_queue_entry(self):
        self.configure_cascade()
        child, path = self.add_cascade_child()
        self.sleep.side_effect = lambda _: self.write_rollout(
            path, child, ('task_started', '2'), ('task_complete', '2'))
        code, result = await self.cascade_close()
        self.assertEqual((code, result['reason'], result['descendant'], result['descendant_state']),
                         (1, 'descendant_turn_unknown', child, 'changed'))

        def populate(_):
            with closing(sqlite3.connect(self.queue)) as db, db:
                db.execute('INSERT INTO queued_items VALUES (?, ?)', (child, 'arrived during settle'))

        self.sleep.side_effect = populate
        code, result = await self.cascade_close()
        self.assertEqual((code, result['reason'], result['descendant']), (1, 'descendant_queue_pending', child))
        self.assertEqual(self.sleep.call_args_list, [mock.call(1)] * 2)
        self.enqueue.assert_not_called()
        self.opener.assert_not_called()

    async def test_cascade_does_not_wait_or_enqueue_past_deadline(self):
        self.configure_cascade()
        self.add_cascade_child()
        code, result = await self.cascade_close(timeout=.9)
        self.assertEqual((code, result['archive_path']), (1, 'not_archived'))
        self.assertIn('deadline', result['reason'])
        self.assertEqual(self.state.route('p', 'target'), self.route)
        self.sleep.assert_not_called()
        self.enqueue.assert_not_called()

    async def test_cascade_rejects_unbound_review_and_requested_held_ids(self):
        self.configure_cascade()
        held, _ = self.add_cascade_child()
        archived, _ = self.add_cascade_child(archived=True)
        snapshot = codex_archive.preview_snapshot(self.db)
        reviewed = sorted([held, archived])
        scope = codex_archive.cascade_digest(self.db, self.route, snapshot, reviewed, [held])
        for review in ({'reviewed': [held], 'deferred': [held], 'held': [held]},
                       {'reviewed': reviewed, 'deferred': [held], 'held': [archived]},
                       {'reviewed': reviewed, 'deferred': [archived], 'held': [archived]}):
            with self.subTest(review=review), self.assertRaises(ValueError):
                await broker.start_self_archive(self.state, {**self.args, 'native_scope': scope, 'cascade': review})
        self.assertEqual(self.state.route('p', 'target'), self.route)
        self.available.assert_not_called()
        self.enqueue.assert_not_called()

    async def test_cascade_rechecks_topology_after_queue_availability(self):
        self.configure_cascade()
        parent, _ = self.add_cascade_child(archived=True)
        child, _ = self.add_cascade_child()

        def reparent(_):
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute('UPDATE thread_spawn_edges SET parent_thread_id=? WHERE child_thread_id=?', (parent, child))

        self.available.side_effect = reparent
        code, result = await self.cascade_close()
        self.assertEqual((code, result['archive_path']), (1, 'not_archived'))
        self.assertIn('subtree or saved profile changed', result['reason'])
        self.sleep.assert_not_called()
        self.enqueue.assert_not_called()

    async def test_legacy_pending_descendant_must_match_exact_archive_after_restart(self):
        child, path = self.add_cascade_child(archived=True)
        self.wake_archiving()
        self.write_rollout(path, self.sid)
        _, scope = codex_archive.native_handoff_scope(self.route)
        pending = await broker.start_self_archive(
            self.state, {**self.args, 'native_scope': scope, 'timeout': .15})
        self.assertEqual(pending['archive_path'], 'pending_self_archive')
        self.assertEqual(json.loads(self.store.path.read_text())['version'], 5)
        self.state = broker.State(store=self.store)
        self.write_rollout(path, child)
        closed = await broker.finish_close(self.state, self.finish_args(pending))
        self.assertEqual((closed['archive_path'], closed['route_removed']), ('self_archive', True))
        self.assert_only_root_entry()

    def write_app_build(self, version, build):
        self.plist.write_bytes(plistlib.dumps({
            'CFBundleShortVersionString': version, 'CFBundleVersion': build,
        }))

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
        self.assertEqual(result['note'], reason)
        self.assertEqual(self.archive_events(), [('not_archived', reason)])
        self.assertEqual(self.state.route('p', 'target'), self.route)
        self.assertEqual(broker.State(store=self.store).route('p', 'target'), self.route)
        self.assertEqual(self.state.messages[self.mid]['state'], 'done')

    def assert_retro_discarded(self, retro):
        self.assertFalse(Path(retro['store']).exists())
        self.assertEqual(list((self.store.directory / 'pending-close-retros').iterdir()), [])

    async def test_exact_entry_archives_and_releases_only_after_readback(self):
        calls = []

        def submit(thread, prompt, timeout):
            self.assertEqual(thread, self.sid)
            self.assertEqual(prompt, 'Archive this Codex Desktop thread now. Call the Codex app '
                             f'set_thread_archived tool with threadId "{self.sid}", hostId "local", '
                             'source "codex", and archived true. Take no other action.')
            self.assertTrue(self.state.endpoints[('p', 'target')]['closing_self_archive'])
            self.assertEqual(self.state.messages[self.mid]['state'], 'done')
            calls.append(('enqueue', thread))
            return self.queue_id

        def open_wake(thread, timeout):
            calls.append(('wake', thread))
            self.record_native_archive()

        self.enqueue.side_effect = submit
        self.opener.side_effect = open_wake
        result = await broker.start_self_archive(self.state, self.args)
        self.assert_closed(result, 'self_archive', 'Queued self-archive verified')
        self.assertEqual(calls, [('enqueue', self.sid), ('wake', self.sid)])
        self.assertIs(result['wake_requested'], True)
        self.assertNotIn('wake_reason', result)

    async def test_queue_preflight_reports_missing_workspace_without_the_path(self):
        self.cwd.rmdir()
        self.available.side_effect = self.real_check_available
        before = self.db.read_bytes()
        with (mock.patch.object(codex_queue, 'default_stores', return_value=(self.db, self.db)),
              mock.patch.object(codex_queue, 'desktop_queue_owner') as owner):
            result = await broker.start_self_archive(self.state, self.args)
        self.assertFalse(self.cwd.exists())
        # Restore the fixture workspace before independently loading the routing checkpoint.
        self.cwd.mkdir()
        reason = 'Codex Desktop queue route cwd no longer exists. No self-archive entry was submitted.'
        self.assert_refused_keeps_open_route(result, reason)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(self.rollout.exists())
        owner.assert_not_called()
        self.enqueue.assert_not_called()
        self.opener.assert_not_called()

    async def test_queue_launch_refusal_reopens_route_without_touching_codex(self):
        self.enqueue.side_effect = self.real_enqueue
        retro = self.prepare_stop_retro()
        before = self.db.read_bytes()
        with mock.patch.object(codex_queue.subprocess, 'Popen',
                               side_effect=OSError('fixture launch failure at /private/codex')) as launch:
            result = await broker.start_self_archive(self.state, {**self.args, 'retro': retro})
        reason = 'Codex Desktop queue CLI could not be started. No self-archive entry was submitted.'
        self.assert_refused_keeps_open_route(result, reason)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(self.rollout.exists())
        self.assert_retro_discarded(retro)
        launch.assert_called_once()
        self.enqueue.assert_called_once()
        self.opener.assert_not_called()
        self.assertIs(result['wake_requested'], False)
        self.assertEqual(result['wake_reason'], 'No queue entry was accepted')

    async def test_queue_holder_conflict_is_returned_as_the_not_archived_reason(self):
        conflict = codex_queue.QueueOwnerUnavailable('Queue holders: pid 42, pid 43.')
        self.available.side_effect = conflict
        result = await broker.start_self_archive(self.state, self.args)
        self.assert_refused_keeps_open_route(result, f'{conflict} No self-archive entry was submitted.')
        self.enqueue.assert_not_called()
        self.opener.assert_not_called()

    async def test_subtree_recheck_error_releases_route_and_staged_retro(self):
        retro = self.prepare_stop_retro()
        reason = 'Codex native close has an unarchived descendant'
        with mock.patch.object(codex_archive, 'native_handoff_scope',
                               side_effect=[(set(), self.args['native_scope']), ValueError(reason)]):
            result = await broker.start_self_archive(self.state, {**self.args, 'retro': retro})
        self.assert_refused_keeps_open_route(result, reason)
        self.assert_retro_discarded(retro)
        self.enqueue.assert_not_called()

    async def test_deadline_before_submission_releases_route_and_staged_retro(self):
        retro = self.prepare_stop_retro()
        with mock.patch.object(broker, 'time', wraps=broker.time) as clock:
            clock.monotonic.return_value = 10

            def expire_after_preflight(route):
                self.assertTrue(route['closing_self_archive'])
                clock.monotonic.return_value = 12

            self.available.side_effect = expire_after_preflight
            result = await broker.start_self_archive(self.state, {**self.args, 'retro': retro})
        self.assert_refused_keeps_open_route(result, 'Codex self-archive deadline expired before queue submission')
        self.assert_retro_discarded(retro)
        self.enqueue.assert_not_called()

    async def test_confirmed_enqueue_that_consumes_close_deadline_still_wakes(self):
        with mock.patch.object(broker, 'time', wraps=broker.time) as clock:
            clock.monotonic.return_value = 10

            def submit(*_):
                clock.monotonic.return_value = 11
                return self.queue_id

            self.enqueue.side_effect = submit
            result = await broker.start_self_archive(self.state, self.args)
        self.assertEqual(result['archive_path'], 'pending_self_archive')
        self.assertIs(result['wake_requested'], True)
        self.opener.assert_called_once()
        self.assertEqual(self.opener.call_args.args[0], self.sid)
        self.assertEqual(codex_queue.verify_signature.call_args.args[-1], 5)

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
        self.opener.assert_not_called()
        self.assertIs(result['wake_requested'], False)

    async def test_unsupported_build_keeps_accepted_entry_and_archive_readback(self):
        self.write_app_build('99.0.1', '1')

        def submit(*_):
            self.record_native_archive()
            return self.queue_id

        self.enqueue.side_effect = submit
        result = await broker.start_self_archive(self.state, self.args)
        self.assert_closed(result, 'self_archive', 'Queued self-archive verified')
        self.enqueue.assert_called_once()
        self.opener.assert_not_called()
        self.assertIs(result['wake_requested'], False)
        self.assertIn('build has not been verified', result['wake_reason'])

    async def test_opener_failure_keeps_close_pending_without_a_second_open(self):
        self.opener.side_effect = OSError('fixture opener failure')
        result = await broker.start_self_archive(self.state, {**self.args, 'timeout': 0.15})
        self.assertEqual(result['archive_path'], 'pending_self_archive')
        self.assertIs(result['wake_requested'], False)
        self.assertIn('could not be confirmed', result['wake_reason'])
        self.record_native_archive()
        finished = await broker.finish_close(self.state, self.finish_args(result))
        self.assert_closed(finished, 'self_archive', 'Queued self-archive verified')
        self.assertIs(finished['wake_requested'], False)
        self.enqueue.assert_called_once()
        self.opener.assert_called_once()

    async def test_timeout_keeps_durable_guard_and_finish_wakes_once_before_verification(self):
        retro = self.prepare_stop_retro()
        result = await broker.start_self_archive(self.state, {**self.args, 'timeout': 0.15, 'retro': retro})
        self.assertEqual(result['archive_path'], 'pending_self_archive')
        self.assertIs(result['wake_requested'], True)
        self.assertFalse(result['route_removed'])
        self.assertEqual(self.archive_events(), [
            ('pending_self_archive', 'Codex self-archive is not yet verified; verify with --finish')])
        self.assertEqual(self.state.messages[self.mid]['state'], 'done')
        self.assertFalse(Path(retro['store']).exists())
        self.assertEqual(set(self.state.messages), {self.mid})
        restored = broker.State(store=self.store)
        self.assertTrue(restored.endpoints[('p', 'target')]['closing_self_archive'])
        self.assertIs(restored.endpoints[('p', 'target')]['closing_self_archive_enqueued'], True)
        self.assertEqual(json.loads(self.store.path.read_text())['version'], 5)
        self.assertEqual(restored.messages[self.mid]['state'], 'done')
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            await broker.start_self_archive(restored, self.args)
        with self.assertRaisesRegex(ValueError, 'does not use a close operator'):
            restored.dispatch_close(self.finish_args(result))
        with self.assertRaisesRegex(ValueError, 'route is closing'):
            restored.route('p', 'target')
        self.state = restored
        self.record_archive_bit(self.rollout)
        calls = mock.Mock()
        calls.attach_mock(self.opener, 'wake')
        with mock.patch.object(broker, 'verify_self_archive_readback',
                               wraps=broker.verify_self_archive_readback) as readback:
            calls.attach_mock(readback, 'verify')
            unverified = await broker.finish_close(restored, {**self.finish_args(result), 'timeout': 0.1})
        self.assertEqual(unverified['archive_path'], 'pending_self_archive')
        self.assertIs(unverified['wake_requested'], True)
        self.assertEqual([call[0] for call in calls.mock_calls[:3]], ['verify', 'wake', 'verify'])
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
        self.assertEqual([call.args[0] for call in self.opener.call_args_list], [self.sid, self.sid])
        self.assertIs(finished['wake_requested'], False)
        self.assertEqual(finished['wake_reason'], 'Thread was already natively archived')

    async def test_uncertain_acceptance_keeps_guard_without_fallback(self):
        self.enqueue.side_effect = broker.DeliveryError('ambiguous queue receipt', uncertain=True)
        result = await broker.start_self_archive(self.state, self.args)
        self.assertEqual(result['archive_path'], 'pending_self_archive')
        self.assertEqual(self.archive_events(), [
            ('pending_self_archive', 'Queue acceptance is uncertain; verify with --finish')])
        self.assertEqual(self.state.endpoints[('p', 'target')]['closing'], result['closing'])
        self.assertEqual(self.state.messages[self.mid]['state'], 'done')
        self.enqueue.assert_called_once()
        self.opener.assert_not_called()
        self.assertIs(result['wake_requested'], False)
        self.assertEqual(result['wake_reason'], 'Queue acceptance is uncertain')
        restored = broker.State(store=self.store)
        self.assertEqual(json.loads(self.store.path.read_text())['version'], 4)
        finished = await broker.finish_close(restored, {**self.finish_args(result), 'timeout': 0.1})
        self.assertEqual(finished['archive_path'], 'pending_self_archive')
        self.assertIs(finished['wake_requested'], False)
        self.assertEqual(finished['wake_reason'], 'Self-archive queue acceptance was not confirmed')
        self.enqueue.assert_called_once()
        self.opener.assert_not_called()

    async def test_unfinished_correspondence_still_blocks_reservation(self):
        notice = self.state.action({'op': 'send', 'project': 'p', 'sender': 'target',
                                    'recipient': 'caller', 'body': 'Ordinary unfinished work', 'key': 'ordinary'})
        with self.assertRaisesRegex(ValueError, 'unfinished correspondence'):
            await broker.start_self_archive(self.state, self.args)
        self.assertEqual(self.state.messages[notice['id']]['state'], 'pending')
        self.assertEqual(self.state.endpoints[('p', 'target')], self.route)
        self.assertEqual(self.archive_events(), [])
        self.enqueue.assert_not_called()

    async def test_close_from_target_workspace_records_stop_retro_once_after_verification(self):
        store = self.cwd.parent / 'retros.jsonl'
        loop = asyncio.get_running_loop()
        caller_cwd = self.cwd / 'nested'
        caller_cwd.mkdir()

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
            snapshot = codex_archive.preview_snapshot(self.db)
            return {'status': 'awaiting_native_archive',
                    'native_scope': codex_archive.cascade_digest(self.db, route, snapshot, [], []),
                    'cascade': {'reviewed': [], 'deferred': [], 'held': []},
                    'results': [{'id': self.sid, 'status': 'awaiting_native_archive'}], 'count': 0}

        def enqueue(*_):
            self.assertFalse(store.exists())
            self.assertEqual(set(self.state.messages), {self.mid})
            self.record_native_archive()

        def scan_cwds(*, skip, cwd_only):
            self.assertTrue(cwd_only)
            self.assertEqual(Path.cwd(), caller_cwd)
            self.assertEqual(skip, (os.getpid(),))
            self.assertEqual(self.state.endpoints[('p', 'target')], self.route)
            rows = [(os.getpid(), 'bridge', str(caller_cwd))]
            return [row for row in rows if row[0] not in skip]

        self.enqueue.side_effect = enqueue
        with mock.patch.object(bridge.broker, 'request', side_effect=request), \
                mock.patch.object(bridge, 'close_session', side_effect=close_session), \
                mock.patch.object(codex_archive.wt, 'open_files', side_effect=scan_cwds) as scan:
            previous_cwd = Path.cwd()
            try:
                os.chdir(caller_cwd)
                code, result = await asyncio.to_thread(
                    run, ['--socket', str(self.cwd.parent / 'fixture.sock'), 'close', 'target', '--project', 'p',
                          '--state-dir', str(self.store.directory), '--archive', '--no-retro', 'fixture',
                          '--urgency', 'stop', '--notify', 'caller', '--store', str(store)])
            finally:
                os.chdir(previous_cwd)
            scan.assert_called_once()
            self.assertEqual((code, result['archive_path'], result['route_removed']), (0, 'self_archive', True))
            self.assertIs(result['wake_requested'], True)
            retry_code, _ = await asyncio.to_thread(
                run, ['--socket', str(self.cwd.parent / 'fixture.sock'), 'close', 'target', '--project', 'p',
                      '--finish', result['close_id'], '--expect-session', self.sid])
        self.assertEqual(retry_code, 1)
        self.opener.assert_called_once()
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
    def test_cli_close_reports_queue_conflict_when_workspace_scan_refuses_first(self):
        conflict = codex_queue.QueueOwnerUnavailable('Queue holders: pid 42, pid 43.')
        with RunningBroker() as server:
            sid = str(uuid.uuid4())
            server.call('register', name='target', project='p', transport='codex',
                        session=sid, cwd=server.temp.name)
            refused = {'session': sid, 'archived': False, 'closed': False, 'count': 0,
                       'results': [{'id': sid, 'status': 'refused',
                                    'refusals': [{'id': sid, 'reason': 'running_process_cwd'}]}]}
            with (mock.patch.object(bridge, 'close_session', return_value=refused),
                  mock.patch.object(codex_queue, 'owner_refusal', return_value=conflict)):
                code, result = run(['--socket', server.path, 'close', 'target', '--project', 'p',
                                    '--state-dir', server.state_dir, '--archive', '--no-retro', 'fixture'])
            self.assertEqual((code, result['status'], result['route_removed']), (1, 'not_archived', False))
            self.assertEqual(result['reason'], f'{conflict} No Desktop queue entry was submitted.')
            self.assertEqual(result['results'], refused['results'])
            self.assertEqual(len(server.call('routes', project='p')), 1)

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
