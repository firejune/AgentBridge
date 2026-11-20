"""Adopt one newly persisted local Codex thread with the proven Desktop wake flow."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import time
import uuid

import codex_preview
import codex_queue

APP_BUILD = ('26.1117.31558', '29513')
APP_SIGNATURE = codex_queue.SIGNATURE.replace('identifier "codex"', 'identifier "com.openai.codex"')
MARKER = 'AB_WAKE_TEST_OK'
PROMPT = (
    'Human-authorized Agent Bridge Desktop wake validation. '
    'Reply exactly AB_WAKE_TEST_OK. Do not use any tools. Validation nonce: '
)
MAX_LINE = 4 * 1024 * 1024
MAX_TAIL = 16 * 1024 * 1024
READ_ONLY_PROFILE = {
    'type': 'managed',
    'file_system': {'type': 'restricted', 'entries': [
        {'path': {'type': 'special', 'value': {'kind': 'root'}}, 'access': 'read'},
    ]},
    'network': 'restricted',
}
CONTEXT_ERROR = 'The supported signed local Codex Desktop queue owner could not be verified'
TARGET_ERROR = 'The exact newly saved Codex thread could not be verified'
EVIDENCE_ERROR = 'The exact Codex Desktop wake turn could not be verified'


class WakeError(ValueError):
    """Public, path-free Desktop wake refusal."""


@dataclass(frozen=True)
class Desktop:
    state: Path
    queue: Path
    owner: tuple[str, str, str]


def budget(timeout):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise WakeError('Codex Desktop wake deadline expired')
    deadline = time.monotonic() + timeout

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise WakeError('Codex Desktop wake deadline expired')
        return left

    return remaining


def _owner(queue, remaining):
    codex_preview.require_owned_store(queue)
    owner = codex_queue.desktop_queue_owner(
        queue, lambda argv: codex_queue.checked_output(argv, timeout=min(2, remaining())))
    if owner is None:
        raise WakeError(CONTEXT_ERROR)
    return owner


def prepare(timeout=10):
    """Validate the sole supported local app and default owner/store before creation."""
    remaining = budget(timeout)
    try:
        state, queue = codex_queue.default_stores()
        app = codex_queue.APP
        with (app / 'Contents/Info.plist').open('rb') as source:
            metadata = plistlib.load(source)
        if (metadata.get('CFBundleShortVersionString'), metadata.get('CFBundleVersion')) != APP_BUILD:
            raise WakeError('This Codex Desktop build has not been verified for queue wake')
        for bundle, signature in ((app, APP_SIGNATURE), (codex_queue.CLI_BUNDLE, codex_queue.SIGNATURE)):
            codex_queue.verify_signature(bundle, signature, min(5, remaining()))
        # The existing public config reader has a bounded independent budget.
        if remaining() < codex_preview.CONFIG_TIMEOUT or codex_preview.state_path(str(codex_queue.CLI)) != state:
            raise WakeError(CONTEXT_ERROR)
        codex_preview.require_owned_store(state)
        return Desktop(state, queue, _owner(queue, remaining))
    except WakeError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise WakeError(CONTEXT_ERROR) from None


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _saved_rollout(desktop, route):
    if (route.get('transport') != 'codex' or not _uuid(route['session'])
            or route.get('executable') not in (None, 'codex', str(codex_queue.CLI))):
        raise WakeError(TARGET_ERROR)
    registered = Path(route['cwd'])
    cwd, archived, recorded_path = codex_preview._saved_thread_row(
        route, include_rollout=True, path=desktop.state)
    if (archived or not registered.is_absolute() or not Path(cwd).is_absolute()
            or not isinstance(recorded_path, str)):
        raise WakeError(TARGET_ERROR)
    cwd = Path(cwd).resolve(strict=True)
    path = Path(recorded_path)
    sessions = desktop.state.parent / 'sessions'
    if (not cwd.is_dir() or cwd != registered.resolve(strict=True) or not path.is_absolute()
            or path.resolve(strict=True) != path or not path.is_relative_to(sessions)
            or not path.name.startswith('rollout-') or not path.name.endswith('-' + route['session'] + '.jsonl')):
        raise WakeError(TARGET_ERROR)
    codex_preview.require_owned_store(path)
    return cwd, path


def _workspace_profile(cwd):
    entries = [
        {'path': {'type': 'special', 'value': {'kind': 'root'}}, 'access': 'read'},
        {'path': {'type': 'path', 'path': str(cwd)}, 'access': 'write'},
        {'path': {'type': 'special', 'value': {'kind': 'slash_tmp'}}, 'access': 'write'},
        {'path': {'type': 'special', 'value': {'kind': 'tmpdir'}}, 'access': 'write'},
    ]
    entries.extend({'path': {'type': 'path', 'path': str(cwd / name)}, 'access': 'read',
                    'missing_path_behavior': 'skip'} for name in ('.git', '.agents', '.codex', '.aws'))
    return {'type': 'managed', 'network': 'restricted',
            'file_system': {'type': 'restricted', 'entries': entries}}


def _permissions(context, cwd):
    profile = context.get('active_permission_profile')
    settings = context.get('permission_profile')
    approval, sandbox = context.get('approval_policy'), context.get('sandbox_policy')
    if (profile == {'id': ':read-only'} and settings == READ_ONLY_PROFILE
            and approval == 'on-request' and sandbox == {'type': 'read-only'}):
        public_sandbox = {'type': 'readOnly'}
    elif (profile == {'id': ':danger-full-access'} and settings == {'type': 'disabled'}
            and approval == 'never' and sandbox == {'type': 'danger-full-access'}):
        public_sandbox = {'type': 'dangerFullAccess'}
    elif (profile == {'id': ':workspace'} and approval == 'on-request'
            and isinstance(sandbox, dict)
            and set(sandbox) == {'type', 'network_access', 'exclude_tmpdir_env_var', 'exclude_slash_tmp'}
            and sandbox['type'] == 'workspace-write'
            and all(sandbox[key] is False for key in ('network_access', 'exclude_tmpdir_env_var', 'exclude_slash_tmp'))
            and settings == _workspace_profile(cwd)):
        public_sandbox = {'type': 'workspaceWrite', 'networkAccess': False}
    else:
        raise WakeError('The Codex Desktop wake turn used unrecognized permission settings')
    return {'activePermissionProfile': profile, 'approvalPolicy': approval, 'sandboxPolicy': public_sandbox}


class _Attestation:
    def __init__(self, thread, cwd, model, proof):
        self.thread, self.cwd, self.model, self.proof = thread, cwd, model, proof
        self.turn = self.permissions = None
        self.saw_input = self.saw_final = False

    def feed(self, record):
        kind, payload = record['type'], record['payload']
        if not isinstance(payload, dict):
            raise WakeError(EVIDENCE_ERROR)
        event = payload.get('type') if kind == 'event_msg' else None
        if event == 'task_started':
            turn, attribution = payload.get('turn_id'), payload.get('turn_attribution')
            if (self.turn is not None or not _uuid(turn) or payload.get('root_turn_id') != turn
                    or not isinstance(attribution, dict) or attribution.get('turn_trigger') != 'queue'
                    or attribution.get('turn_id') != turn or attribution.get('root_turn_id') != turn
                    or attribution.get('parent_turn_id') is not None):
                raise WakeError(EVIDENCE_ERROR)
            self.turn = turn
        elif kind == 'turn_context':
            if (self.turn is None or self.permissions is not None or payload.get('turn_id') != self.turn
                    or payload.get('cwd') != str(self.cwd)):
                raise WakeError(EVIDENCE_ERROR)
            if payload.get('model') != self.model:
                raise WakeError('The Codex Desktop wake turn used a different model')
            self.permissions = _permissions(payload, self.cwd)
        elif event in ('model_reroute', 'error', 'turn_aborted'):
            raise WakeError(EVIDENCE_ERROR)
        elif event == 'item_completed':
            item = payload.get('item')
            if (self.permissions is None or payload.get('thread_id') != self.thread
                    or payload.get('turn_id') != self.turn or not isinstance(item, dict)):
                raise WakeError(EVIDENCE_ERROR)
            if item.get('type') == 'UserMessage':
                if (self.saw_input or self.saw_final or item.get('content') != [
                        {'type': 'text', 'text': self.proof, 'text_elements': []}]):
                    raise WakeError(EVIDENCE_ERROR)
                self.saw_input = True
            elif item.get('type') == 'AgentMessage':
                if (not self.saw_input or self.saw_final or item.get('phase') != 'final_answer'
                        or item.get('content') != [{'type': 'Text', 'text': MARKER}]):
                    raise WakeError(EVIDENCE_ERROR)
                self.saw_final = True
            else:
                raise WakeError(EVIDENCE_ERROR)
        elif event == 'task_complete':
            if (not self.saw_final or payload.get('turn_id') != self.turn
                    or payload.get('root_turn_id') != self.turn or payload.get('last_agent_message') != MARKER):
                raise WakeError(EVIDENCE_ERROR)
            return {'model': self.model, 'permission_settings': self.permissions, 'turn_id': self.turn}
        elif kind == 'response_item' and payload.get('type') not in ('additional_tools', 'message', 'reasoning'):
            raise WakeError(EVIDENCE_ERROR)
        return None


def _record(line):
    item = json.loads(line)
    if not isinstance(item, dict):
        raise WakeError(EVIDENCE_ERROR)
    return item


def _identity(info):
    return info.st_dev, info.st_ino


def _tail(source, path, initial, parser, remaining):
    while True:
        remaining()
        observed = path.lstat()
        position = source.tell()
        if _identity(observed) != _identity(initial) or observed.st_size < position:
            raise WakeError(EVIDENCE_ERROR)
        line = source.readline(MAX_LINE + 1)
        if len(line) > MAX_LINE or position + len(line) - initial.st_size > MAX_TAIL:
            raise WakeError(EVIDENCE_ERROR)
        if not line.endswith(b'\n'):
            source.seek(position)
            time.sleep(min(.1, remaining()))
            continue
        result = parser.feed(_record(line))
        if result is not None:
            return result


def adopt(preflight, route, expected_model, timeout):
    """Queue one nonce-bound message and require fresh, exact Desktop turn evidence."""
    remaining = budget(timeout)
    try:
        cwd, path = _saved_rollout(preflight, route)
        thread = route['session']
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), 'rb') as source:
            initial = os.fstat(source.fileno())
            if (not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.getuid()
                    or initial.st_mode & 0o022 or _identity(path.lstat()) != _identity(initial)
                    or initial.st_size < 1 or initial.st_size > MAX_TAIL):
                raise WakeError(TARGET_ERROR)
            line = source.readline(MAX_LINE + 1)
            if not line.endswith(b'\n') or len(line) > MAX_LINE:
                raise WakeError(TARGET_ERROR)
            metadata = _record(line)
            if (metadata.get('type') != 'session_meta' or not isinstance(metadata.get('payload'), dict)
                    or metadata['payload'].get('id') != thread or metadata['payload'].get('cwd') != str(cwd)):
                raise WakeError(TARGET_ERROR)
            source.seek(initial.st_size - 1)
            if source.read(1) != b'\n' or os.fstat(source.fileno()).st_size != initial.st_size:
                raise WakeError(TARGET_ERROR)
            if _owner(preflight.queue, remaining) != preflight.owner:
                raise WakeError(CONTEXT_ERROR)
            proof = PROMPT + str(uuid.uuid4())
            codex_queue.enqueue(thread, proof, remaining())
            subprocess.run(['/usr/bin/open', '-a', str(codex_queue.APP), f'codex://threads/wake/{thread}'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=remaining(), check=True)
            result = _tail(source, path, initial, _Attestation(thread, cwd, expected_model, proof), remaining)
            if _owner(preflight.queue, remaining) != preflight.owner or _saved_rollout(preflight, route) != (cwd, path):
                raise WakeError(CONTEXT_ERROR)
            remaining()
            return result
    except WakeError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise WakeError(EVIDENCE_ERROR) from None
