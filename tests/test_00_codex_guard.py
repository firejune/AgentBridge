"""Install the launch guard during discovery, including for Python children."""

import atexit
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
import uuid

GUARD = Path(__file__).resolve().parent / 'fixtures' / 'no_live_codex'
runpy.run_path(str(GUARD / 'sitecustomize.py'))
pythonpath = os.environ.get('PYTHONPATH')
os.environ['PYTHONPATH'] = os.pathsep.join(filter(None, (str(GUARD), pythonpath)))
codex_home = tempfile.TemporaryDirectory(prefix='ab-guard-', dir='/tmp')
atexit.register(codex_home.cleanup)
os.environ['CODEX_HOME'] = os.environ['CODEX_SQLITE_HOME'] = codex_home.name


class CodexLaunchGuardTests(unittest.TestCase):
    def test_discovery_process_blocks_wake_and_queue(self):
        commands = (
            (['/usr/bin/open', 'codex://threads/wake/' + str(uuid.uuid4())], 'wake opener'),
            (['/Applications/Codex.app/Contents/Resources/codex', 'queue'], 'queue runner'),
        )
        for argv, boundary in commands:
            with self.subTest(boundary=boundary):
                with self.assertRaisesRegex(AssertionError, f'mock the Codex Desktop {boundary}'):
                    sys.audit('subprocess.Popen', argv[0], argv, None, None)

    def test_python_children_block_the_real_launch_boundaries(self):
        code = """
import sys
sys.path.insert(0, sys.argv[1])
from bridge import codex_queue
import codex_wake_open

def refuse_any_launch(event, payload):
    if event == 'subprocess.Popen':
        raise AssertionError('Guard test fallback reached')

# If the inherited guard regresses, this probe still cannot launch anything.
sys.addaudithook(refuse_any_launch)
thread = sys.argv[2]
if sys.argv[3] == 'wake opener':
    codex_wake_open._open_wake_url(thread, 1)
else:
    codex_queue.enqueue(thread, 'test fixture', 1)
"""
        scripts = Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'
        for boundary in ('wake opener', 'queue runner'):
            with self.subTest(boundary=boundary):
                result = subprocess.run(
                    [sys.executable, '-c', code, str(scripts), str(uuid.uuid4()), boundary],
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f'AssertionError: Test must mock the Codex Desktop {boundary}', result.stderr)
