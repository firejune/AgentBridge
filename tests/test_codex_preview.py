import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import codex_preview


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'
CODEX_STATE = re.compile(r'state_path|state_5|STATE_FILE')
WRITABLE = re.compile(r'mode=rw')
CONNECT = re.compile(r'sqlite3\.connect\(')


def writable_codex_state(source):
    """Lines that could open Codex state writable: Agent Bridge never writes it (#212)."""
    if not CODEX_STATE.search(source):
        return []
    return [(number, line.strip()) for number, line in enumerate(source.splitlines(), 1)
            if WRITABLE.search(line) or (CONNECT.search(line) and 'mode=ro' not in line)]


class CodexStateReadOnlyGuard(unittest.TestCase):
    def test_no_script_opens_codex_state_writable(self):
        found = {path.name: rows for path in sorted(SCRIPTS.glob('*.py'))
                 if (rows := writable_codex_state(path.read_text(encoding='utf-8')))}
        self.assertEqual(found, {})

    def test_guard_catches_a_writable_open(self):
        planted = ("path = state_path(binary)\n"
                   "sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)\n"
                   "sqlite3.connect(path)\n"
                   "sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)\n")
        self.assertEqual([number for number, _ in writable_codex_state(planted)], [2, 3])
        self.assertEqual(writable_codex_state("sqlite3.connect(ledger)\n"), [])

    def test_the_preview_writer_is_gone(self):
        self.assertFalse(hasattr(codex_preview, 'initialize'))


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
