"""Opt-in compatibility checks against the installed Codex Desktop on macOS."""
from contextlib import closing, contextmanager
from mmap import ACCESS_READ, mmap
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import codex_live
import codex_preview
import codex_queue
import codex_wake_open


@contextmanager
def bundle_bytes(path):
    with path.open('rb') as source, mmap(source.fileno(), 0, access=ACCESS_READ) as data:
        yield data


@unittest.skipUnless(os.getenv('AGENT_BRIDGE_LIVE_CANARY') == '1',
                     'set AGENT_BRIDGE_LIVE_CANARY=1 to inspect the installed Codex Desktop')
class CodexDesktopCanary(unittest.TestCase):
    def setUp(self):
        if sys.platform != 'darwin':
            self.skipTest('requires the installed macOS Codex Desktop')

    def test_installed_build_matches_wake_pin(self):
        pin = 'codex_wake_open.py APP_BUILD'
        try:
            with (codex_queue.APP / 'Contents/Info.plist').open('rb') as source:
                metadata = plistlib.load(source)
        except (OSError, ValueError, plistlib.InvalidFileException):
            self.fail(f'{pin}: could not read the installed app Info.plist')
        observed = metadata.get('CFBundleShortVersionString'), metadata.get('CFBundleVersion')
        self.assertEqual(observed, codex_wake_open.APP_BUILD, f'{pin}: installed version/build drifted')

    def test_running_desktop_queue_owner_matches_parser(self):
        pin = 'codex_queue.py default_stores / desktop_queue_owner'
        try:
            _, queue = codex_queue.default_stores()
            codex_preview.require_owned_store(queue)
            owner = codex_queue.desktop_queue_owner(queue)
        except subprocess.CalledProcessError as error:
            self.fail(f'{pin}: {Path(error.cmd[0]).name} could not identify the queue owner; ensure Desktop is running')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.fail(f'{pin}: could not read the default owned queue store or process metadata')
        self.assertTrue(owner is not None, f'{pin}: no unique Desktop app-server matched the owner pin')

    def test_saved_thread_store_matches_queue_lookup(self):
        pin = 'codex_preview.py STATE_FILE / _saved_thread_row'
        try:
            state, _ = codex_queue.default_stores()
            self.assertEqual({path.name for path in state.parent.glob('state_*.sqlite')}, {state.name},
                             f'{pin}: installed state store filename drifted')
            codex_preview.require_owned_store(state)
            # The immutable connection reads the main file without touching WAL/SHM.
            # LIMIT 0 checks the checkpointed schema without retrieving any thread data.
            with closing(sqlite3.connect(state.as_uri() + '?mode=ro&immutable=1', uri=True)) as database:
                database.execute('SELECT cwd, archived, rollout_path FROM threads WHERE id = ? LIMIT 0', ('',))
        except (OSError, ValueError, sqlite3.Error):
            self.fail(f'{pin}: checkpointed state store cannot serve the saved-thread lookup')

    def test_bundled_queue_receipt_matches_enqueue_parser(self):
        pin = 'codex_queue.py enqueue receipt parser'
        # Rust inline fmt stores literal fragments between control bytes and argument
        # slots. Recover the fragments together, rather than finding unrelated strings.
        try:
            with bundle_bytes(codex_queue.CLI) as data:
                templates = set(re.findall(rb'(Queued message [\x20-\x7e]*)\xc0[\x01-\x7f]'
                                           rb'([\x20-\x7e]+)\xc0[\x01-\x7f]'
                                           rb'([\x20-\x7e]+)\x00', data))
        except (OSError, ValueError):
            self.fail(f'{pin}: could not read the bundled CLI executable')
        self.assertEqual(len(templates), 1, f'{pin}: could not identify one bundled receipt template')
        prefix, middle, suffix = (part.decode('ascii') for part in templates.pop())
        queued, thread = str(uuid.uuid4()), str(uuid.uuid4())
        process = Mock(returncode=0, **{'communicate.return_value':
                                      (f'{prefix}{queued}{middle}{thread}{suffix}\n', None),
                                      'poll.return_value': 0})
        with patch.object(codex_queue.subprocess, 'Popen', return_value=process):
            try:
                result = codex_queue.enqueue(thread, 'canary fixture', 1)
            except codex_live.DeliveryError:
                self.fail(f'{pin}: parser rejected the receipt rendered from the bundled CLI')
        self.assertEqual(result, queued, f'{pin}: parsed the wrong queued ID')

    def test_bundled_idle_steer_error_matches_fallback(self):
        pin = 'codex_live.py deliver inactive error comparison'
        archive = codex_queue.APP / 'Contents/Resources/app.asar'
        try:
            with bundle_bytes(archive) as data:
                templates = set(re.findall(rb'Error\(`(Cannot steer conversation )\$\{[\w$]+\}'
                                           rb'([^\x60\r\n]{1,120})`\)\)\.name=', data))
        except (OSError, ValueError):
            self.fail(f'{pin}: could not read the Desktop app.asar')
        if not templates:
            self.skipTest(f'{pin}: no identifiable throw template in the installed app.asar')
        self.assertEqual(len(templates), 1, f'{pin}: bundled idle error templates disagree')
        prefix, suffix = (part.decode('utf-8') for part in templates.pop())
        thread, message, turn = (str(uuid.uuid4()) for _ in range(3))
        route = {'session': thread, 'cwd': '/fixture'}
        with (patch.object(codex_live, 'exact_saved_workspace', return_value=(None, None)),
              patch.object(codex_live, 'Client') as client_class):
            client = client_class.return_value
            client.owner.return_value = 'fixture-owner'
            client.call.side_effect = [
                {'resultType': 'error', 'error': prefix + thread + suffix},
                {'resultType': 'success', 'result': {'result': {'turnId': turn}}},
            ]
            try:
                result = codex_live.deliver(route, message, 'canary fixture', allow_queue=False)
            except codex_live.DeliveryError:
                self.fail(f'{pin}: delivery rejected the idle error rendered from app.asar')
        self.assertEqual([call.args[0] for call in client.call.call_args_list],
                         ['thread-follower-steer-turn', 'thread-follower-start-turn'], pin)
        self.assertEqual((result['delivery_mode'], result['turn_id']), ('idle', turn), pin)
