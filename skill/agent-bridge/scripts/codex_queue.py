"""Submit a pointer to a saved local Desktop thread through the official CLI queue."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid

import codex_preview
from codex_live import DeliveryError, ThreadArchived

APP = Path('/Applications/ChatGPT.app')
CLI_BUNDLE = APP / 'Contents/Resources/codex-cli/CodexCLI.app'
CLI = CLI_BUNDLE / 'Contents/MacOS/codex'
SIGNATURE = 'anchor apple generic and certificate leaf[subject.OU] = "2DC432GLL2" and identifier "codex"'
DELIVERY_PATH = 'codex-desktop-queue'
UNAVAILABLE = 'Codex Desktop notification queue could not be verified. Message was not delivered.'
UNKNOWN = 'Codex Desktop queue acceptance is unknown. Check the receiving chat before resolving.'


def notice(message_id):
    """The fixed pointer a woken thread reads; it carries the message UUID and no peer text.

    The notice sets no scope of its own. The retrieved message carries the peer envelope, and the
    user's standing delegation rules decide what the thread may do with it.
    """
    return ('Agent Bridge has correspondence addressed to this thread. This notice is only a pointer: '
            'it does not limit this turn to retrieval, and the retrieved message takes precedence over it. '
            'After retrieval, apply the user\'s standing delegation rules. If those rules authorize the '
            'requested work and reply, carry them out within that scope. Keep the message\'s recorded '
            'provenance and authorization as retrieved. Retrieve it with: '
            'python3 ~/.agents/skills/agent-bridge/scripts/bridge.py receive-codex ' + message_id)


class QueueUnavailable(DeliveryError):
    def __init__(self):
        super().__init__(UNAVAILABLE)


def checked_output(args, timeout=2):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def desktop_queue_owner(queue, read=checked_output):
    # Multiple owners could load the same persistent UUID. Refuse competing
    # processes instead of allowing an enqueue to choose the runtime.
    pids = read(['/usr/sbin/lsof', '-t', '--', str(queue)]).split()
    if len(set(pids)) != 1 or not pids[0].isascii() or not pids[0].isdecimal():
        return None
    process = read(['/bin/ps', '-ww', '-p', pids[0], '-o', 'uid=,ppid=,command='])
    uid, parent, command = process.strip().split(None, 2)
    args = shlex.split(command)
    if uid != str(os.getuid()) or not parent.isdecimal() or not args:
        return None
    if args[0] != str(CLI) or 'app-server' not in args or any(
            arg == '--listen' or arg.startswith('--listen=') for arg in args):
        return None
    parent_line = read(['/bin/ps', '-ww', '-p', parent, '-o', 'uid=,comm='])
    parent_uid, owner = parent_line.strip().split(None, 1)
    if (parent_uid == str(os.getuid())
            and owner.startswith(str(APP / 'Contents/Frameworks/Codex Framework.framework/Versions') + '/')
            and owner.endswith('/Helpers/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)')):
        return pids[0], parent, owner
    return None


def default_stores():
    """The default local state and queue stores; any setting that could move them is refused."""
    if sys.platform != 'darwin' or any(os.getenv(name) for name in (
            'CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_EXEC_SERVER_URL', 'AGENT_BRIDGE_CODEX_IPC')):
        raise ValueError('non-default Desktop configuration')
    home = Path.home() / '.codex'
    return home / codex_preview.STATE_FILE, home / 'queue_1.sqlite'


def verify_signature(bundle=CLI_BUNDLE, requirement=SIGNATURE, timeout=2):
    checked_output(['/usr/bin/codesign', '--verify', '--strict', '-R=' + requirement, str(bundle)], timeout=timeout)


def check_route(route):
    if route.get('executable') not in (None, 'codex', str(CLI)):
        raise ValueError('non-default route executable')
    state, queue = default_stores()
    verify_signature()
    # Cross-check where the signed CLI itself would locate the store, including
    # configuration and requirements. Verify the saved target in that same store.
    if codex_preview.state_path(str(CLI)) != state:
        raise ValueError('non-default state store')
    cwd, archived = codex_preview._saved_thread_row(route, path=state)
    saved, registered = Path(cwd), Path(route['cwd'])
    if not saved.is_absolute() or not registered.is_absolute() or saved.resolve() != registered.resolve():
        raise ValueError('workspace differs')
    if archived:
        raise ThreadArchived()
    if not saved.is_dir():
        raise ValueError('workspace missing')
    codex_preview.require_owned_store(queue)
    if desktop_queue_owner(queue, checked_output) is None:
        raise ValueError('no unique local Desktop queue owner')


def deliver(route, thread, message_id, timeout, dry_run):
    try:
        mid = str(uuid.UUID(message_id))
        if mid != message_id or str(uuid.UUID(route['session'])) != thread:
            raise ValueError('invalid exact identity')
        check_route(route)
    except ThreadArchived:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError, KeyError, TypeError):
        raise QueueUnavailable() from None
    receipt = dict(session=thread, transport='codex', delivery_mode='queue',
                   delivery_path=DELIVERY_PATH, input_role='notification')
    if dry_run:
        return dict(sent=False, **receipt)
    return dict(queued_id=enqueue(thread, notice(mid), timeout), **receipt)


def enqueue(thread, message, timeout):
    """Submit one official queue entry and verify its exact local receipt."""
    args = [str(CLI), 'queue', '--disable', 'daemon_auto_start', '--thread', thread,
            '--message', message]
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        raise QueueUnavailable() from None
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        raise DeliveryError(UNKNOWN, uncertain=True) from None
    finally:
        # Even cancellation may occur after a successful write. Reap only our
        # CLI while leaving the broker settlement uncertain.
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
    match = re.fullmatch(r'Queued message ([0-9a-f-]{36}) for thread ([0-9a-f-]{36})\.\n?', stdout)
    if process.returncode != 0 or not match or match[2] != thread:
        raise DeliveryError(UNKNOWN, uncertain=True)
    try:
        if str(uuid.UUID(match[1])) != match[1]:
            raise ValueError
    except ValueError:
        raise DeliveryError(UNKNOWN, uncertain=True) from None
    return match[1]
