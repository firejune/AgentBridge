"""Stop real Codex Desktop wake and queue launches in unittest processes."""

import os
import sys


def block_live_codex(event, payload):
    if event != 'subprocess.Popen':
        return
    executable, argv, _, _ = payload
    command = os.path.basename(os.fsdecode(executable))
    args = tuple(map(os.fsdecode, argv))
    if command == 'open' and any(arg.startswith('codex://threads/wake/') for arg in args):
        raise AssertionError('Test must mock the Codex Desktop wake opener')
    if command == 'codex' and args[1:2] == ('queue',):
        raise AssertionError('Test must mock the Codex Desktop queue runner')


sys.addaudithook(block_live_codex)
