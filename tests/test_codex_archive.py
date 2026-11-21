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
                     'updated_at INTEGER, archived INTEGER, archived_at INTEGER)')
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
        self.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)',
                     (sid, f'title {n}', str(cwd or self.cwd), age, bit, 2 if bit else None))
        if parent:
            self.execute('INSERT INTO thread_spawn_edges VALUES (?, ?)', (parent, sid))
        return sid

    def set_bit(self, method, sid):
        bit = int(method == 'thread/archive')
        self.execute('UPDATE threads SET archived=?, archived_at=? WHERE id=?',
                     (bit, 2 if bit else None, sid))

    def rpc(self, method, params):
        sid = params['threadId']
        self.calls.append((method, sid))
        self.behavior(method, sid)
        return {}  # The response alone carries no completion evidence.

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
            writer.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)',
                           (sid, 'WAL fixture', str(self.cwd), 1, 0, None))
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
        real_copy = archive.shutil.copyfile

        def change(source, target):
            result = real_copy(source, target)
            self.add(2)
            return result

        with patch.object(archive.shutil, 'copyfile', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'changed during preview'):
                self.plan()


    def test_lsof_cwd_probe_keeps_parentheses_in_workspace_names(self):
        with patch.object(archive.wt.subprocess, 'Popen') as popen:
            process = popen.return_value
            process.communicate.return_value = ('p123\ncworker\nn/work (copy)/nested\n', None)
            process.pid, process.returncode = 456, 0
            self.assertEqual(self.real_open_files(cwd_only=True), [(123, 'worker', '/work (copy)/nested')])
            self.assertEqual(popen.call_args.args[0][-2:], ['-d', 'cwd'])
