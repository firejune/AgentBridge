from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import codex_preview


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-preview-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'state_5.sqlite'
        self.route = {'session': str(uuid.uuid4()), 'cwd': str(self.root)}
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, preview TEXT NOT NULL DEFAULT '', cwd TEXT, first_user_message TEXT, title TEXT, originator TEXT)")
            c.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)',
                      (self.route['session'], '', str(self.root), '', 'Original title', 'agent_bridge'))
        self.resolver = patch.object(codex_preview, 'state_path', return_value=self.path)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)

    def row(self):
        with closing(sqlite3.connect(self.path)) as c, c:
            return c.execute('SELECT * FROM threads').fetchone()

    def test_first_task_sets_list_eligible_preview_without_human_identity(self):
        before = self.row()
        text = '한글 첫 과제 🌉\nQuotes " SQL ? ; <task> 그대로'
        self.assertEqual(codex_preview.initialize(self.route, text), {'status': 'initialized'})
        self.assertEqual(self.row(), (before[0], text, *before[2:]))
        with closing(sqlite3.connect(self.path)) as c, c:
            self.assertEqual(c.execute("SELECT id FROM threads WHERE preview <> ''").fetchall(),
                             [(self.route['session'],)])
        self.assertEqual(codex_preview.initialize(self.route, 'second task'), {'status': 'preserved'})
        self.assertEqual(self.row()[1], text)

    def test_native_timestamp_triggers_do_not_block_preview(self):
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('ALTER TABLE threads ADD COLUMN updated_at INTEGER DEFAULT 0')
            c.execute('ALTER TABLE threads ADD COLUMN updated_at_ms INTEGER')
            c.execute("""CREATE TRIGGER threads_updated_at_ms_after_update
                AFTER UPDATE OF updated_at ON threads
                WHEN NEW.updated_at != OLD.updated_at
                 AND NEW.updated_at_ms IS OLD.updated_at_ms
                BEGIN UPDATE threads SET updated_at_ms = NEW.updated_at * 1000 WHERE id = NEW.id; END""")
            c.execute("""CREATE TRIGGER threads_updated_at_ms_after_insert
                AFTER INSERT ON threads WHEN NEW.updated_at_ms IS NULL
                BEGIN UPDATE threads SET updated_at_ms = NEW.updated_at * 1000 WHERE id = NEW.id; END""")
        before=self.row()
        self.assertEqual(codex_preview.initialize(self.route,'task'), {'status':'initialized'})
        self.assertEqual(self.row(),(before[0],'task',*before[2:]))

    def test_existing_native_preview_is_preserved(self):
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('UPDATE threads SET preview=?, first_user_message=?', ('human preview', 'human message'))
        before = self.row()
        self.assertEqual(codex_preview.initialize(self.route, 'task'), {'status': 'preserved'})
        self.assertEqual(self.row(), before)

    def test_missing_store_never_created(self):
        self.path.unlink()
        self.assertEqual(codex_preview.initialize(self.route, 'task')['status'], 'unavailable')
        self.assertFalse(self.path.exists())

    def test_locked_missing_row_schema_and_trigger_fail_closed(self):
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('BEGIN EXCLUSIVE')
            self.assertEqual(codex_preview.initialize(self.route, 'task')['status'], 'unavailable')
        self.assertEqual(self.row()[1], '')
        self.assertEqual(codex_preview.initialize({**self.route, 'session':str(uuid.uuid4())}, 'task')['status'], 'unavailable')
        self.assertEqual(codex_preview.initialize({**self.route, 'cwd':'/wrong'}, 'task')['status'], 'unavailable')
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('CREATE TRIGGER extra AFTER UPDATE ON threads BEGIN UPDATE threads SET first_user_message=NEW.preview; END')
        self.assertEqual(codex_preview.initialize(self.route, 'task')['status'], 'unavailable')
        self.assertEqual(self.row()[1:2], ('',))
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('DROP TABLE threads')
            c.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, preview BLOB, cwd TEXT, first_user_message TEXT)')
        self.assertEqual(codex_preview.initialize(self.route, 'task')['status'], 'unavailable')

    def test_simultaneous_tasks_fill_only_once(self):
        barrier = threading.Barrier(2)
        results=[]
        def initialize(text):
            barrier.wait()
            results.append(codex_preview.initialize(self.route, text))
        threads=[threading.Thread(target=initialize,args=(text,)) for text in ('one', 'two')]
        for t in threads: t.start()
        for t in threads: t.join(2)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(sorted(r['status'] for r in results), ['initialized','preserved'])
        self.assertIn(self.row()[1], ('one','two'))

    def test_config_failures_are_sanitized(self):
        with patch.object(codex_preview, 'state_path', side_effect=ValueError('SECRET_TASK')):
            result=codex_preview.initialize(self.route,'SECRET_TASK')
        self.assertEqual(result['status'],'unavailable')
        self.assertNotIn('SECRET_TASK',str(result))


class StatePathTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ab-preview-config-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'state_5.sqlite').touch()
        self.client=MagicMock()
        self.client.__enter__.return_value=self.client
        self.factory=patch.object(codex_preview.sessions,'CodexClient',return_value=self.client).start()
        self.addCleanup(patch.stopall)

    def resolve(self, config=None, requirement=None, environ=None):
        self.client.call.side_effect=[{'config':{'sqlite_home':config}}, {'requirements':{'sqliteHome':requirement}}]
        with patch.dict(os.environ,environ or {},clear=True):
            return codex_preview.state_path('custom-codex')

    def test_config_requirement_and_environment_precedence(self):
        self.assertEqual(self.resolve(config=str(self.root)),self.root/'state_5.sqlite')
        self.assertEqual(self.resolve(config='/wrong',environ={'CODEX_SQLITE_HOME':str(self.root)}),self.root/'state_5.sqlite')
        self.assertEqual(self.resolve(config='/wrong',requirement=str(self.root),environ={'CODEX_SQLITE_HOME':'/wrong'}),self.root/'state_5.sqlite')
        self.assertEqual(self.resolve(environ={'CODEX_HOME':str(self.root)}),self.root/'state_5.sqlite')
        self.assertEqual(self.factory.call_args.kwargs['executable'],'custom-codex')
        self.assertEqual([x.args[0] for x in self.client.call.call_args_list[-2:]], ['config/read','configRequirements/read'])

    def test_unknown_state_version_and_relative_paths_refused(self):
        (self.root/'state_6.sqlite').touch()
        with self.assertRaises(ValueError):self.resolve(config=str(self.root))
        with self.assertRaises(ValueError):self.resolve(config='relative')
