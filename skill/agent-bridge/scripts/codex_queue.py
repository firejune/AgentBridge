"""Submit a pointer to a saved local Desktop thread through the official CLI queue."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid

import codex_preview
from codex_live import DeliveryError, ThreadArchived, close_notice

APP = Path('/Applications/ChatGPT.app')
CLI_BUNDLE = APP / 'Contents/Resources/codex-cli/CodexCLI.app'
CLI = CLI_BUNDLE / 'Contents/MacOS/codex'
NODE_REPL = APP / 'Contents/Resources/cua_node/bin/node_repl'
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
            'provenance and authorization as retrieved. If a tool denial showed this exact message in '
            'the current turn, use that text now, even if receive-codex returns already_retrieved. '
            'Use the included reread command if the denial says the body was truncated. '
            'The same message ID may appear in both places. Skip any message you have already acted on. '
            'If no matching denial appeared in this turn, run the command below, keep its output visible, '
            'and act only on returned text. If you see neither a command result nor a matching denial, '
            'run the same read command once more unless the hook told you to stop. Retrieve it with: '
            'python3 ~/.agents/skills/agent-bridge/scripts/bridge.py receive-codex ' + message_id)


class QueueUnavailable(DeliveryError):
    def __init__(self, summary=UNAVAILABLE, *, detail=None):
        super().__init__(detail if detail is not None else summary)
        self.summary = summary


class QueueOwnerUnavailable(QueueUnavailable):
    SUMMARY = 'No unique local Desktop queue owner.'

    def __init__(self, detail):
        super().__init__(self.SUMMARY, detail=f'{self.SUMMARY} {detail}')


class QueueWorkspaceMismatch(QueueUnavailable):
    def __init__(self):
        super().__init__('Codex Desktop queue saved workspace differs from the route cwd.')


# Read-only probes (ps, lsof, codesign). Two seconds was not enough on a loaded machine (#239).
PROBE_TIMEOUT = 10


def checked_output(args, timeout=PROBE_TIMEOUT):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def desktop_helper(path):
    return (path.startswith(str(APP / 'Contents/Frameworks/Codex Framework.framework/Versions') + '/')
            and path.endswith('/Helpers/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)'))


def process_rows(read, pids, columns):
    try:
        return read(['/bin/ps', '-ww', '-p', ','.join(pids), '-o', columns]).splitlines()
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def app_server_args(command):
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    if not args or (args[0] != 'codex' and os.path.normpath(args[0]) != str(CLI)):
        return None
    args = args[1:]
    while len(args) >= 2 and args[0] in ('-c', '--config'):
        args = args[2:]
    return args[1:] if args[:1] == ['app-server'] else None


def queue_holder_detail(pids, read):
    if not pids:
        return 'No process holds the queue store.'
    holders = {}
    for line in process_rows(read, pids, 'pid=,uid=,ppid=,etime=,command='):
        fields = line.split(None, 4)
        if len(fields) == 5 and fields[0] in pids and re.fullmatch(r'[0-9]+', fields[2]):
            holders[fields[0]] = fields[1:]
    executables = {}
    process_ids = sorted(set(holders) | {row[1] for row in holders.values() if row[0] == str(os.getuid())})
    if process_ids:
        for line in process_rows(read, process_ids, 'pid=,uid=,comm='):
            fields = line.split(None, 2)
            if len(fields) == 3 and fields[0] in process_ids and fields[1] == str(os.getuid()):
                executables[fields[0]] = fields[2]
    details = []
    cua = False
    for pid in pids:
        uid, parent, elapsed, command = holders.get(pid, ('', 'unknown', '', ''))
        role = 'unverified'
        args = app_server_args(command)
        if uid == str(os.getuid()) and executables.get(pid) == str(CLI) and args is not None:
            role = 'bundled app-server'
            if desktop_helper(executables.get(parent, '')) and not any(
                    arg == '--listen' or arg.startswith('--listen=') for arg in args):
                role = 'Desktop app-server'
            elif executables.get(parent) == str(NODE_REPL) and args in (
                    ['--listen', 'stdio://'], ['--listen=stdio://']):
                role = 'bundled node_repl app-server (CUA)'
                cua = True
        age = elapsed if re.fullmatch(r'(?:[0-9]{1,10}-)?(?:[0-9]{2}:)?[0-9]{2}:[0-9]{2}',
                                     elapsed) else 'unknown'
        details.append(f'pid {pid} ({role}, parent pid {parent}, elapsed {age})')
    detail = 'Queue holders: ' + ', '.join(details) + '.'
    if cua and len(pids) > 1:
        detail += (' A bundled node_repl app-server (CUA) is a known cause of this conflict. '
                   'Stopping that server restores delivery once the Desktop app-server is the only holder. '
                   'Stop it only with the owner\'s approval.')
    return detail


def desktop_queue_owner(queue, read=checked_output, *, explain=False):
    # Multiple owners could load the same persistent UUID. Refuse competing
    # processes instead of allowing an enqueue to choose the runtime.
    try:
        pids = set(read(['/usr/sbin/lsof', '-t', '--', str(queue)]).split())
    except subprocess.CalledProcessError as error:
        # lsof returns 1 with empty output when no process holds an existing file.
        if not explain:
            raise
        if error.returncode != 1 or error.stdout or error.stderr:
            raise QueueOwnerUnavailable('The queue holder list could not be read.') from None
        pids = set()
    except (OSError, subprocess.SubprocessError, ValueError):
        if not explain:
            raise
        raise QueueOwnerUnavailable('The queue holder list could not be read.') from None
    if any(not re.fullmatch(r'[1-9][0-9]{0,9}', pid) for pid in pids):
        if explain:
            raise QueueOwnerUnavailable('The queue holder list could not be verified.')
        return None
    if len(pids) == 1:
        pid = next(iter(pids))
        try:
            owner = desktop_process_owner(pid, read)
        except (OSError, subprocess.SubprocessError, ValueError):
            if not explain:
                raise
        else:
            if owner is not None:
                return owner
    if explain:
        raise QueueOwnerUnavailable(queue_holder_detail(sorted(pids, key=int), read))
    return None


def desktop_process_owner(pid, read):
    process = read(['/bin/ps', '-ww', '-p', pid, '-o', 'uid=,ppid=,command='])
    uid, parent, command = process.strip().split(None, 2)
    args = shlex.split(command)
    if (uid != str(os.getuid()) or not re.fullmatch(r'[0-9]+', parent)
            or not args or args[0] != str(CLI) or 'app-server' not in args
            or any(arg == '--listen' or arg.startswith('--listen=') for arg in args)):
        return None
    parent_line = read(['/bin/ps', '-ww', '-p', parent, '-o', 'uid=,comm='])
    parent_uid, owner = parent_line.strip().split(None, 1)
    if parent_uid == str(os.getuid()) and desktop_helper(owner):
        return pid, parent, owner
    return None


def default_stores():
    """The default local state and queue stores; any setting that could move them is refused."""
    if sys.platform != 'darwin' or any(os.getenv(name) for name in (
            'CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_EXEC_SERVER_URL', 'AGENT_BRIDGE_CODEX_IPC')):
        raise ValueError('non-default Desktop configuration')
    home = Path.home() / '.codex'
    return home / codex_preview.STATE_FILE, home / 'queue_1.sqlite'


def owner_refusal():
    """Read-only diagnosis for a caller that already reached a blocked queue path."""
    try:
        _, queue = default_stores()
        codex_preview.require_owned_store(queue)
        desktop_queue_owner(queue, checked_output, explain=True)
    except QueueOwnerUnavailable as error:
        return error
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def verify_signature(bundle=CLI_BUNDLE, requirement=SIGNATURE, timeout=PROBE_TIMEOUT):
    checked_output(['/usr/bin/codesign', '--verify', '--strict', '-R=' + requirement, str(bundle)], timeout=timeout)


@contextmanager
def queue_check(summary):
    """Normalize provider failures at the check that observed them."""
    try:
        yield
    except (ThreadArchived, QueueUnavailable):
        raise
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError, KeyError, TypeError):
        raise QueueUnavailable(summary) from None


def missing_workspace(cwd):
    return QueueUnavailable(
        'Codex Desktop queue route cwd no longer exists.',
        detail=f"route cwd {cwd} no longer exists; the Codex thread's saved cwd must exist for queue delivery")


def check_route(route):
    if route.get('executable') not in (None, 'codex', str(CLI)):
        raise QueueUnavailable('Codex Desktop queue requires the default route executable.')
    with queue_check('Codex Desktop queue requires the default Desktop configuration.'):
        state, queue = default_stores()
    with queue_check('Codex Desktop queue CLI signature check failed.'):
        verify_signature()
    # Cross-check where the signed CLI itself would locate the store, including
    # configuration and requirements. Verify the saved target in that same store.
    with queue_check('Codex Desktop queue could not verify the native state store configuration.'):
        configured_state = codex_preview.state_path(str(CLI))
    if configured_state != state:
        raise QueueUnavailable('Codex Desktop queue requires the default native state store.')
    saved, archived = check_saved_route(route, state)
    if archived:
        raise ThreadArchived()
    if not saved.is_dir():
        raise missing_workspace(Path(route['cwd']))
    with queue_check('Codex Desktop queue store is unavailable or is not owned and protected.'):
        codex_preview.require_owned_store(queue)
    if desktop_queue_owner(queue, checked_output, explain=True) is None:
        raise QueueOwnerUnavailable('The queue holder could not be verified.')


def check_saved_route(route, state):
    with queue_check('Codex Desktop queue could not verify the saved thread metadata.'):
        if route.get('closing_self_archive'):
            with codex_preview.private_store_copy(state) as copy:
                cwd, archived = codex_preview._saved_thread_row(route, path=copy)
        else:
            cwd, archived = codex_preview._saved_thread_row(route, path=state)
    with queue_check('Codex Desktop queue could not verify the saved and registered workspaces.'):
        saved, registered = Path(cwd), Path(route['cwd'])
        if not saved.is_absolute() or not registered.is_absolute() or saved.resolve() != registered.resolve():
            raise QueueWorkspaceMismatch()
    return saved, archived


def check_available(route):
    with queue_check(UNAVAILABLE):
        check_route(route)


def enqueue_self_archive(route, timeout):
    thread = route['session']
    if str(uuid.UUID(thread)) != thread:
        raise ValueError('invalid exact identity')
    prompt = ('Archive this Codex Desktop thread now. Call the Codex app set_thread_archived tool '
              f'with threadId "{thread}", hostId "local", source "codex", and archived true. '
              'Take no other action.')
    return enqueue(thread, prompt, timeout)


def deliver(route, thread, message_id, timeout, dry_run, *, close_handoff=False, before_queue=None):
    try:
        mid = str(uuid.UUID(message_id))
        if mid != message_id or str(uuid.UUID(route['session'])) != thread:
            raise ValueError('invalid exact identity')
    except (ValueError, KeyError, TypeError):
        raise QueueUnavailable('Codex Desktop queue could not verify the exact delivery identity.') from None
    check_available(route)
    receipt = dict(session=thread, transport='codex', delivery_mode='queue',
                   delivery_path=DELIVERY_PATH, input_role='notification')
    if dry_run:
        return dict(sent=False, **receipt)
    if before_queue is not None:
        before_queue()
    notification = (close_notice(mid, 'python3 ~/.agents/skills/agent-bridge/scripts/bridge.py close-operator ' + mid)
                    if close_handoff else notice(mid))
    return dict(queued_id=enqueue(thread, notification, timeout), **receipt)


def enqueue(thread, message, timeout):
    """Submit one official queue entry and verify its exact local receipt."""
    args = [str(CLI), 'queue', '--disable', 'daemon_auto_start', '--thread', thread,
            '--message', message]
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        raise QueueUnavailable('Codex Desktop queue CLI could not be started.') from None
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
