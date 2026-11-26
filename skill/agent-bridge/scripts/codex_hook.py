"""Plan and apply a reversible edit to the user's Codex hooks.json."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

from route_store import atomic_write, sync_directory

MARKER = 'Agent Bridge cooperative stop'
MAX_CONFIG = 1024 * 1024
WHITESPACE = re.compile(r'\s*')


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('hooks.json contains duplicate keys')
    return result


def load(text):
    config = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(config, dict) or not isinstance(config.get('hooks', {}), dict):
        raise ValueError('Expected a hooks.json object containing a hooks object')
    if not isinstance(config.get('hooks', {}).get('PreToolUse', []), list):
        raise ValueError('Expected hooks.PreToolUse to be an array')
    return config


def member_start(text, start, key):
    """Locate a member of an already validated JSON object."""
    decode = json.JSONDecoder().raw_decode
    index = start + 1
    while True:
        index = WHITESPACE.match(text, index).end()
        name, index = decode(text, index)
        index = text.index(':', index) + 1
        index = WHITESPACE.match(text, index).end()
        if name == key:
            return index
        _, index = decode(text, index)
        index = text.index(',', index) + 1


def group_for(socket, entrypoint):
    script = Path(entrypoint).absolute().with_name('codex_stop_hook.py')
    if not script.is_file():
        script = Path(__file__).with_name('codex_stop_hook.py')
    command = shlex.join([sys.executable, '-B', str(script), '--socket',
                          str(Path(socket).expanduser().absolute())])
    return {'hooks': [{'type': 'command', 'command': command, 'timeout': 2, 'statusMessage': MARKER}]}


def snippets(group):
    entry = json.dumps(group, ensure_ascii=True, separators=(',', ':'))
    event = f'"PreToolUse":[{entry}]'
    hooks = f'"hooks":{{{event}}}'
    return entry, event, hooks


def insertion(payload, has_members):
    return '\n  ' + payload + (',' if has_members else '') + '\n'


def managed_groups(groups):
    for group in groups:
        handlers = group.get('hooks') if isinstance(group, dict) else None
        if isinstance(handlers, list) and any(isinstance(hook, dict) and hook.get('statusMessage') == MARKER
                                              for hook in handlers):
            yield group


def require_managed_shape(group):
    handlers = group['hooks']
    if set(group) == {'hooks'} and len(handlers) == 1 and isinstance(handlers[0], dict):
        hook = handlers[0]
        command = hook.get('command')
        if (set(hook) == {'type', 'command', 'timeout', 'statusMessage'} and hook['type'] == 'command'
                and hook['timeout'] == 2 and isinstance(command, str)):
            args = shlex.split(command)
            if (len(args) == 5 and args[1] == '-B' and args[3] == '--socket'
                    and Path(args[2]).name == 'codex_stop_hook.py'):
                return
    raise ValueError('Agent Bridge hook was changed. Remove the marked entry manually.')


def edit(text, action, group):
    config = {} if text is None else load(text)
    hooks = config.get('hooks', {})
    groups = hooks.get('PreToolUse', [])
    owned = list(managed_groups(groups))
    if len(owned) > 1:
        raise ValueError('Multiple Agent Bridge hook entries require manual review')
    if action == 'status':
        return {'installed': bool(owned)}
    if action == 'install' and owned:
        if owned != [group]:
            raise ValueError('Agent Bridge hook differs from this install. Uninstall it first.')
        return {'installed': True}
    if action == 'uninstall' and not owned:
        return {'installed': False}
    if owned:
        require_managed_shape(owned[0])

    entry, event, root = snippets(owned[0] if owned else group)
    if action == 'install' and text is None:
        return {'create': '{' + root + '}\n'}
    if action == 'uninstall' and text == '{' + root + '}\n':
        return {'delete': text}

    start = len(text) - len(text.lstrip())
    locations = [(start + 1, root, bool(config))]
    if 'hooks' in config:
        hooks_at = member_start(text, start, 'hooks')
        locations.append((hooks_at + 1, event, bool(hooks)))
    if 'PreToolUse' in hooks:
        event_at = member_start(text, hooks_at, 'PreToolUse')
        locations.append((event_at + 1, entry, bool(groups)))
    if action == 'install':
        offset, payload, has_members = locations[-1]
        return {'offset': offset, 'insert': insertion(payload, has_members)}

    groups.remove(owned[0])
    expected = [deepcopy(config)]
    if not groups:
        del hooks['PreToolUse']
        expected.append(deepcopy(config))
        if not hooks:
            del config['hooks']
            expected.append(config)
    for offset, payload, _ in locations:
        for has_members in (True, False):
            removed = insertion(payload, has_members)
            if not text.startswith(removed, offset):
                continue
            try:
                after = load(text[:offset] + text[offset + len(removed):])
            except ValueError:
                continue
            if after in expected:
                return {'offset': offset, 'remove': removed}
    raise ValueError('Agent Bridge hook formatting was changed. Remove the marked entry manually.')


def read_config(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, 0o600
    with os.fdopen(fd, 'rb') as source:
        meta = os.fstat(source.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o022:
            raise ValueError('hooks.json must be an owner-controlled regular file')
        data = source.read(MAX_CONFIG + 1)
    if len(data) > MAX_CONFIG:
        raise ValueError('hooks.json exceeds the size limit')
    return data.decode('utf-8'), stat.S_IMODE(meta.st_mode)


@contextmanager
def directory_lock(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if meta.st_uid != os.getuid() or meta.st_mode & 0o022:
            raise ValueError('Codex config directory must be owner-controlled')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def configure(action, apply, socket, entrypoint):
    directory = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()
    path = directory / 'hooks.json'
    if apply and action == 'install':
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Lock the directory across read and replace to serialize this CLI's edits
    # without adding any control file or locking a replaceable inode.
    lock = directory_lock(directory) if apply and directory.exists() else nullcontext()
    with lock:
        text, mode = read_config(path)
        plan = edit(text, action, group_for(socket, entrypoint))
        changed = any(key in plan for key in ('create', 'insert', 'remove', 'delete'))
        if apply and changed:
            if 'delete' in plan:
                path.unlink()
                sync_directory(directory)
            else:
                after = plan.get('create')
                if after is None:
                    offset = plan['offset']
                    after = text[:offset] + plan.get('insert', '') + text[offset + len(plan.get('remove', '')):]
                atomic_write(path, after.encode('utf-8'), prefix='.agent-bridge-hook-', mode=mode)
        return {'path': str(path), 'changed': changed, 'applied': apply and changed, **plan}
