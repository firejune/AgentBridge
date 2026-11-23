from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

import bridge
import codex_archive as archive


class CodexArchiveTests(unittest.TestCase):
    def setUp(self):
        root = self.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(root).resolve()
        self.db = self.root / 'state_5.sqlite'
        self.routes = self.root / 'bridge.state'
        self.routes.mkdir(mode=0o700)
        self.prefix = self.root / 'work'
        self.cwd = self.prefix / 'gone'
        self.calls = []
        self.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT, '
                     'updated_at INTEGER, archived INTEGER, archived_at INTEGER, rollout_path TEXT)')
        # Codex keeps a thread's rollout in sessions/ and moves it to archived_sessions/ when it archives.
        self.sessions, self.archived = self.root / 'sessions', self.root / 'archived_sessions'
        self.sessions.mkdir()
        self.archived.mkdir()
        self.enter_context(patch.dict(archive.os.environ, {'CODEX_HOME': str(self.root)}))
        self.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)')
        self.enter_context(patch.object(archive, 'executable', return_value='/fixture/codex'))
        self.enter_context(patch.object(archive.codex_preview, 'state_path', return_value=self.db))
        self.real_open_files = archive.wt.open_files
        self.holders = self.enter_context(patch.object(archive.wt, 'open_files', return_value=[]))
        native = self.enter_context(patch.object(archive.sessions, 'CodexClient'))
        native.return_value.__enter__.return_value.call.side_effect = self.rpc
        self.native = native
        self.behavior = self.set_bit

    # unittest.TestCase.enterContext is only available starting with Python 3.11.
    def enter_context(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(sql, params)

    def add(self, n, *, parent=None, cwd=None, age=1, bit=0):
        sid = str(uuid.UUID(int=n))
        rollout = (self.archived if bit else self.sessions) / f'{sid}.jsonl'
        rollout.write_text('transcript', encoding='utf-8')
        self.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (sid, f'title {n}', str(cwd or self.cwd), age, bit, 2 if bit else None, str(rollout)))
        if parent:
            self.execute('INSERT INTO thread_spawn_edges VALUES (?, ?)', (parent, sid))
        return sid

    def set_bit(self, method, sid):
        # A native archive sets the bit and moves the rollout; an unarchive moves it back.
        bit = int(method == 'thread/archive')
        source, target = (self.sessions, self.archived) if bit else (self.archived, self.sessions)
        rollout = target / f'{sid}.jsonl'
        if (source / rollout.name).exists():
            (source / rollout.name).rename(rollout)
        self.execute('UPDATE threads SET archived=?, archived_at=?, rollout_path=? WHERE id=?',
                     (bit, 2 if bit else None, str(rollout), sid))

    def set_bit_only(self, sid):
        # What Agent Bridge used to write from outside: the bit without a native archive (#212).
        self.execute('UPDATE threads SET archived=1, archived_at=2 WHERE id=?', (sid,))

    def rpc(self, method, params):
        sid = params['threadId']
        self.calls.append((method, sid))
        self.behavior(method, sid)
        return {}  # The response alone carries no completion evidence.

    def bits(self, *sids):
        with closing(sqlite3.connect(self.db)) as db:
            return [db.execute('SELECT archived FROM threads WHERE id=?', (sid,)).fetchone()[0] for sid in sids]

    def plan(self):
        return archive.plan(self.db, self.routes, self.prefix)

    def apply(self, manifest):
        return archive.apply(self.db, self.routes, self.prefix, manifest, 1)

    def route(self, sid, name='worker', cwd=None):
        return dict(project='fixture', name=name, transport='codex', cwd=str(cwd or self.cwd),
                    session=sid, executable=None)

    def register(self, *routes):
        path = self.routes / 'routes.json'
        path.write_text(json.dumps({'version': 1, 'endpoints': routes, 'channels': []}))
        path.chmod(0o600)

    def test_manifest_scope_refusals_and_read_only(self):
        old = self.add(1)
        outside = self.add(2, cwd=self.root / 'work-other')
        blocked_parent = self.add(3)
        self.add(4, parent=blocked_parent, cwd=self.root / 'work-other')
        existing = self.prefix / 'existing'
        existing.mkdir(parents=True)
        live = self.add(5, cwd=existing)
        self.holders.return_value = [(12, 'shell', str(existing / 'nested'))]
        retained = self.add(6)
        self.register(self.route(retained))
        recent = self.add(7, age=archive.time.time())
        extant = self.add(8, cwd=self.prefix)
        with patch.object(bridge.codex_archive, 'sweep_store', return_value=self.db), \
                patch.object(bridge, 'output') as output:
            self.assertEqual(bridge.main(['archive-sweep', '--state-dir', str(self.routes),
                                          '--cwd-prefix', str(self.prefix)]), 0)
        manifest = output.call_args.args[0]
        self.assertEqual([row['id'] for row in manifest['candidates']], [old])
        self.assertFalse(any(row['id'] == outside for row in manifest['refusals']))
        reasons = {row['id']: row['reason'] for row in manifest['refusals']}
        self.assertEqual(reasons[live], 'running_process_cwd')
        self.assertEqual(reasons[retained], 'registered_route')
        self.assertEqual(reasons[recent], 'recent_activity')
        self.assertEqual(reasons[extant], 'running_process_cwd')
        group = next(row for row in manifest['refusals'] if row['id'] == blocked_parent)
        self.assertEqual(group['refusals'][0]['reason'], 'outside_cwd_prefix')
        self.native.assert_not_called()
        self.assertFalse(any(row['archived'] for row in archive.Snapshot(self.db).rows.values()))

    def test_leaf_first_and_existing_cwd_exemptions(self):
        existing = self.prefix / 'existing'
        existing.mkdir(parents=True)
        root = self.add(1)
        leaf = self.add(2, parent=root, cwd=existing)
        unsupported = self.add(3, cwd=existing)
        archived = self.add(4, bit=1)
        orphan = self.add(5, parent=archived, cwd=existing)
        manifest = self.plan()
        self.assertEqual([row['id'] for row in manifest['candidates']], [root, orphan])
        self.assertEqual(manifest['refusals'], [archive.describe(
            archive.Snapshot(self.db).rows[unsupported], 'existing_cwd_without_archived_parent')])
        result = self.apply(manifest)
        self.assertEqual((result['count'], result['complete']), (3, True))
        self.assertEqual(self.calls, [('thread/archive', sid) for sid in (leaf, root, orphan)])
        self.assertTrue(all(' unarchive ' in row['undo'] for row in result['results']))
        self.native.assert_called_with(executable='/fixture/codex', mode='saved', timeout=1)

    def test_close_leaf_first_with_existing_cwd(self):
        self.cwd.mkdir(parents=True)
        root = self.add(1)
        leaf = self.add(2, parent=root)
        route = self.route(root)
        self.register(route)
        result = archive.close(route, self.routes, 1)
        self.assertEqual(self.calls, [('thread/archive', leaf), ('thread/archive', root)])
        self.assertEqual((result['closed'], result['count'], result['archived_at']), (True, 2, 2))
        self.assertIn('Native Codex archive verified', result['note'])

    def test_close_archives_recent_root_and_child_rejected_by_sweep(self):
        now = int(archive.time.time())
        root = self.add(1, age=now - 3)
        child = self.add(2, parent=root, age=now - 1)
        manifest = self.plan()
        self.assertEqual(manifest['candidates'], [])
        self.assertEqual({row['id']: row['reason'] for row in manifest['refusals']},
                         {root: 'recent_activity', child: 'recent_activity'})
        route = self.route(root)
        self.register(route)

        result = archive.close(route, self.routes, 1)

        self.assertEqual(self.calls, [('thread/archive', child), ('thread/archive', root)])
        self.assertEqual((result['closed'], result['count'], result['archive_incomplete']),
                         (True, 2, False))

    def test_close_ignores_desktop_helpers_but_not_other_processes(self):
        self.cwd.mkdir(parents=True)
        root = self.add(1)
        route = self.route(root)
        helper, uid = str(archive.DESKTOP_HELPER), archive.os.getuid()
        listing = f'  41 900 {uid} {helper}\n  42 900 {uid} {helper}\n'
        owner = patch.object(archive.codex_queue, 'desktop_queue_owner', return_value=('900', '899', 'Codex'))
        stores = patch.object(archive.codex_queue, 'default_stores', return_value=(self.db, self.db))
        ps = patch.object(archive.codex_queue, 'checked_output', return_value=listing)
        helpers = [(41, 'node_repl', str(self.cwd)), (42, 'node_repl', str(self.cwd))]

        # The sweep exempts nothing: the same helpers keep the thread out of a manifest.
        self.holders.return_value = helpers
        with owner, stores, ps:
            self.assertEqual({row['reason'] for row in self.plan()['refusals']}, {'running_process_cwd'})
        self.register(route)

        cases = (('another process beside the helpers', helpers + [(43, 'zsh', str(self.cwd))], listing),
                 ('a node_repl the Desktop owner did not start', helpers, listing.replace(' 900 ', ' 901 ', 1)),
                 ('a node_repl that is another binary', helpers, listing.replace(helper, '/usr/local/bin/node_repl', 1)))
        for label, rows, table in cases:
            self.holders.return_value = rows
            with self.subTest(label), owner, stores, patch.object(archive.codex_queue, 'checked_output',
                                                                   return_value=table):
                result = archive.close(route, self.routes, 1)
                self.assertEqual(result['results'][0]['refusals'][0]['reason'], 'running_process_cwd')
                self.assertEqual(self.calls, [])
        self.holders.return_value = helpers
        with stores, ps, patch.object(archive.codex_queue, 'desktop_queue_owner', return_value=None):
            self.assertFalse(archive.close(route, self.routes, 1)['closed'])
        self.assertEqual(self.calls, [])

        # While the archive client runs, Desktop is not the store's only owner: the helpers
        # found before it started are the ones the recheck exempts.
        with stores, ps, patch.object(archive.codex_queue, 'desktop_queue_owner',
                                      side_effect=[('900', '899', 'Codex'), None, None]):
            result = archive.close(route, self.routes, 1)
        self.assertEqual(self.calls, [('thread/archive', root)])
        self.assertEqual((result['closed'], result['count']), (True, 1))

    def test_close_never_writes_codex_state_when_desktop_holds_the_writer(self):
        def held(method, sid):
            raise archive.sessions.CodexRpcError(method, {
                'code': -32600, 'message': f'thread {sid} already has an active writer'})

        self.behavior = held
        # A sweep gets no fallback: the rejected thread stays unarchived.
        swept = self.add(5)
        result = self.apply(self.plan())['results']
        self.assertEqual((result[0]['status'], self.bits(swept)), ('not_archived', [0]))

        self.cwd.mkdir(parents=True)
        root = self.add(1)
        route = self.route(root)
        self.register(route)
        before = self.db.read_bytes()
        result = archive.close(route, self.routes, 1)
        self.assertEqual(self.calls[-1], ('thread/archive', root))
        self.assertEqual((result['closed'], result['archived'], result['archive_incomplete']), (False, False, True))
        receipt, = result['results']
        self.assertEqual((receipt['status'], receipt['reason']), ('not_archived', 'desktop_holds_thread'))
        self.assertNotIn('method', receipt)
        self.assertIn('Desktop holds this thread', result['note'])
        self.assertEqual(self.db.read_bytes(), before)  # Agent Bridge wrote nothing to Codex state (#212).
        self.assertEqual(self.bits(root, swept), [0, 0])
        self.assertTrue((self.sessions / f'{root}.jsonl').is_file())

        # Any other rejection is still a failed archive.
        other = self.add(3, cwd=self.root / 'other')
        (self.root / 'other').mkdir()
        route = self.route(other, 'second', self.root / 'other')
        self.register(self.route(root), route)

        def rejected(method, sid):
            raise archive.sessions.CodexRpcError(method, {'code': -32600, 'message': 'invalid request'})

        self.behavior = rejected
        result = archive.close(route, self.routes, 1)
        self.assertFalse(result['closed'])
        self.assertEqual(result['note'], 'Native Codex archive was not verified. Closing route kept.')
        self.assertEqual(self.bits(other), [0])

    def test_close_reports_held_child_without_attempting_parent_or_native_handoff(self):
        self.cwd.mkdir(parents=True)
        root = self.add(1)
        child = self.add(2, parent=root)
        route = self.route(root)
        self.register(route)
        before = self.db.read_bytes()

        def held(method, sid):
            raise archive.sessions.CodexRpcError(method, {
                'code': -32600, 'message': f'thread {sid} already has an active writer'})

        self.behavior = held
        with patch.object(archive, 'sweep_store', return_value=self.db):
            result = archive.close(route, self.routes, 1, prefer_native=lambda: True)

        self.assertEqual(self.calls, [('thread/archive', child)])
        self.assertEqual((result['closed'], result['archived'], result['archive_incomplete'], result['count']),
                         (False, False, True, 0))
        self.assertNotIn('native_scope', result)
        self.assertEqual(result['note'], 'Codex Desktop holds an unarchived descendant. '
                                         'The parent was not archived and the route is kept.')
        child_receipt, parent_receipt = result['results']
        self.assertEqual((child_receipt['id'], child_receipt['status'], child_receipt['reason']),
                         (child, 'not_archived', 'desktop_holds_thread'))
        self.assertEqual(parent_receipt, {'id': root, 'status': 'descendant_not_archived'})
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue((self.sessions / f'{child}.jsonl').is_file())
        self.assertTrue((self.sessions / f'{root}.jsonl').is_file())

    def test_close_reports_a_bit_only_archive_as_not_closed(self):
        # The bit is set but the rollout is still in sessions/: Codex never archived the thread (#212).
        self.cwd.mkdir(parents=True)
        for label, behavior, preset in (('native call sets only the bit', lambda method, sid: self.set_bit_only(sid), False),
                                        ('thread already half-archived', None, True)):
            with self.subTest(label):
                self.calls.clear()
                sid = self.add(10 if preset else 11)
                if preset:
                    self.set_bit_only(sid)
                self.behavior = behavior or self.set_bit
                route = self.route(sid)
                self.register(route)
                result = archive.close(route, self.routes, 1)
                self.assertEqual((result['closed'], result['archived'], result['archive_incomplete']),
                                 (False, False, True))
                self.assertEqual((result['results'][-1]['status'], result['results'][-1]['reason']),
                                 ('not_archived', 'rollout_not_archived'))
                self.assertTrue((self.sessions / f'{sid}.jsonl').is_file())
                self.register()

    def test_close_refuses_alias_and_protected_descendant(self):
        root = self.add(1)
        leaf = self.add(2, parent=root)
        route = self.route(root)
        for other in (self.route(root, 'alias'), self.route(leaf, 'leaf')):
            with self.subTest(other=other):
                self.register(route, other)
                result = archive.close(route, self.routes, 1)
                self.assertEqual((result['archived'], result['archive_incomplete']), (False, True))
        self.assertEqual(self.calls, [])

    def test_manifest_digest_and_changed_set_or_new_protection(self):
        root = self.add(1)
        manifest = self.plan()
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self.apply(dict(manifest, candidates=[]))
        leaf = self.add(2, parent=root)
        result = self.apply(manifest)
        self.assertEqual(result['results'], [{'id': root, 'status': 'descendants_changed'}])
        manifest = self.plan()
        self.register(self.route(leaf))
        result = self.apply(manifest)
        self.assertEqual(result['results'][0]['reason'], 'registered_route')
        self.assertFalse(result['complete'])
        self.assertEqual(self.calls, [])

    def test_planted_empty_reply_partial_continues_independent_branch(self):
        root = self.add(1)
        child = self.add(2, parent=root)
        independent = self.add(3)

        def partial(method, sid):
            if sid != child:
                self.set_bit(method, sid)

        self.behavior = partial
        result = self.apply(self.plan())
        self.assertEqual([(r['id'], r['status']) for r in result['results']],
                         [(child, 'not_archived'), (root, 'descendant_not_archived'), (independent, 'archived')])
        self.assertEqual((result['count'], result['complete'], result['stopped']), (1, False, False))
        self.assertEqual(self.calls, [('thread/archive', child), ('thread/archive', independent)])

    def test_planted_cascade_partial_from_late_child_and_provider_error(self):
        root = self.add(1)
        manifest = self.plan()

        def partial(method, sid):
            self.set_bit(method, sid)
            self.add(2, parent=root)  # Late child whose archive move failed.
            raise archive.sessions.CodexRpcError(method, {'code': -32000, 'message': 'fixture'})

        self.behavior = partial
        result = self.apply(manifest)['results'][0]
        self.assertEqual(result['provider_error'], {'kind': 'rpc_rejected', 'code': -32000})
        self.assertTrue(result['archived'])
        self.assertEqual(result['status'], 'not_archived')
        self.assertEqual(result['not_archived_descendants'], [str(uuid.UUID(int=2))])

    def test_late_descendant_is_restored_using_its_own_uuid(self):
        root = self.add(1)
        late = str(uuid.UUID(int=2))

        def cascade(method, sid):
            self.set_bit(method, sid)
            if sid == root:
                self.add(2, parent=root, bit=1)

        self.behavior = cascade
        result = self.apply(self.plan())
        self.assertEqual(self.calls, [('thread/archive', root), ('thread/unarchive', late)])
        self.assertEqual(result['results'][0]['restored_unlisted'], [late])
        self.assertTrue(result['complete'])
        self.assertFalse(archive.Snapshot(self.db).rows[late]['archived'])

    def test_failed_restore_stops_before_independent_candidate(self):
        root = self.add(1)
        self.add(3)
        late = str(uuid.UUID(int=2))

        def cascade(method, sid):
            if sid == root:
                self.set_bit(method, sid)
                self.add(2, parent=root, bit=1)
            else:
                raise archive.sessions.CodexRpcError(method, {'code': -32000, 'message': 'fixture'})

        self.behavior = cascade
        result = self.apply(self.plan())
        self.assertEqual(self.calls, [('thread/archive', root), ('thread/unarchive', late)])
        self.assertEqual((result['complete'], result['stopped']), (False, True))
        failure = result['results'][0]
        self.assertEqual((failure['status'], failure['unrestored']), ('restore_failed', [late]))
        self.assertEqual(failure['restore_provider_errors'][0]['kind'], 'rpc_rejected')

    def test_restore_cascade_detects_previously_archived_collateral(self):
        root = self.add(1)
        prior = self.add(3, bit=1)

        def cascade(method, sid):
            self.set_bit(method, sid)
            if sid == root:
                late = self.add(2, parent=root, bit=1)
                self.execute('INSERT INTO thread_spawn_edges VALUES (?, ?)', (late, prior))
            else:
                self.set_bit(method, prior)

        self.behavior = cascade
        result = self.apply(self.plan())
        self.assertTrue(result['stopped'])
        self.assertEqual(result['results'][0]['collateral'], [prior])

    def test_apply_cli_requires_reviewed_file_and_inherits_manifest_scope(self):
        root = self.add(1)
        manifest = self.plan()
        path = self.root / 'reviewed.json'
        path.write_text(json.dumps(manifest))
        args = ['archive-sweep', '--state-dir', str(self.routes)]
        with patch.object(bridge.codex_archive, 'sweep_store', return_value=self.db), \
                patch.object(bridge, 'output') as output:
            self.assertEqual(bridge.main([*args, '--apply']), 1)
            self.assertIn('--manifest FILE', output.call_args.args[0]['error'])
            self.assertEqual(bridge.main([*args, '--apply', '--manifest', str(path)]), 0)
            self.assertEqual(output.call_args.args[0]['digest'], manifest['digest'])
        self.assertEqual(self.calls, [('thread/archive', root)])

    def test_rechecks_route_before_parent_call(self):
        root = self.add(1)
        leaf = self.add(2, parent=root)

        def route_appears(method, sid):
            self.set_bit(method, sid)
            self.register(self.route(root))

        self.behavior = route_appears
        result = self.apply(self.plan())
        self.assertEqual(self.calls, [('thread/archive', leaf)])
        self.assertEqual([(r['status'], r.get('reason')) for r in result['results']],
                         [('archived', None), ('refused', 'registered_route')])
        self.assertFalse(result['complete'])

    def test_reparenting_inside_same_root_set_invalidates_manifest(self):
        root = self.add(1)
        branch = self.add(2, parent=root)
        leaf = self.add(3, parent=root)
        manifest = self.plan()
        self.execute('UPDATE thread_spawn_edges SET parent_thread_id=? WHERE child_thread_id=?',
                     (branch, leaf))
        result = self.apply(manifest)
        self.assertFalse(result['complete'])
        self.assertEqual(result['results'][0]['status'], 'descendants_changed')
        self.assertEqual(self.calls, [])

    def test_preview_reads_wal_without_creating_native_sidecars(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            sid = str(uuid.UUID(int=1))
            writer.execute('INSERT INTO threads (id, title, cwd, updated_at, archived, archived_at) '
                           'VALUES (?, ?, ?, ?, ?, ?)', (sid, 'WAL fixture', str(self.cwd), 1, 0, None))
            writer.commit()
            wal = self.db.with_name(self.db.name + '-wal')
            self.assertGreater(wal.stat().st_size, 0)
            self.assertEqual([row['id'] for row in self.plan()['candidates']], [sid])
        before = set(self.db.parent.iterdir())
        self.assertNotIn(wal, before)
        self.assertEqual([row['id'] for row in self.plan()['candidates']], [sid])
        self.assertEqual(set(self.db.parent.iterdir()), before)

    def test_preview_refuses_database_changed_during_copy(self):
        self.add(1)
        real_copy = archive.codex_preview.shutil.copyfile

        writes = iter(range(2, 100))

        def change(source, target):
            result = real_copy(source, target)
            self.add(next(writes))
            return result

        with patch.object(archive.codex_preview.shutil, 'copyfile', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'changed during preview'):
                self.plan()


    def test_preview_retries_a_copy_that_raced_one_write(self):
        sid = self.add(1)
        real_copy = archive.codex_preview.shutil.copyfile
        calls = []

        def change_once(source, target):
            result = real_copy(source, target)
            if not calls:
                self.add(2)
            calls.append(source)
            return result

        with patch.object(archive.codex_preview.shutil, 'copyfile', side_effect=change_once):
            self.assertIn(sid, [row['id'] for row in self.plan()['candidates']])
        self.assertGreater(len(calls), 1)

    def test_lsof_cwd_probe_keeps_parentheses_in_workspace_names(self):
        with patch.object(archive.wt.subprocess, 'Popen') as popen:
            process = popen.return_value
            process.communicate.return_value = ('p123\ncworker\nn/work (copy)/nested\n', None)
            process.pid, process.returncode = 456, 0
            self.assertEqual(self.real_open_files(cwd_only=True), [(123, 'worker', '/work (copy)/nested')])
            self.assertEqual(popen.call_args.args[0][-2:], ['-d', 'cwd'])
