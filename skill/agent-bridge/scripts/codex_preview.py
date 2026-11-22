"""Read-only access to Codex's native state_5 store.

Agent Bridge never writes Codex state (#212): every connection here opens the
store read-only, and archive status is verified as Codex itself records it.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
from subprocess import TimeoutExpired
import time
import uuid

import sessions

CONFIG_TIMEOUT = 2
LOCK_TIMEOUT = .1
STATE_FILE = 'state_5.sqlite'


def state_path(executable=None):
    """Use the same installed CLI/config/environment as saved session discovery."""
    deadline = time.monotonic() + CONFIG_TIMEOUT
    with sessions.CodexClient(executable=executable or 'codex', mode='saved',
                              timeout=CONFIG_TIMEOUT, experimental=True) as client:
        def read(method, params):
            client.timeout = max(.001, deadline - time.monotonic())
            if time.monotonic() >= deadline:
                raise ValueError('configuration deadline exceeded')
            return client.call(method, params)
        config = read('config/read', {'includeLayers': False}).get('config')
        requirements = read('configRequirements/read', {}).get('requirements')
    if not isinstance(config, dict) or (requirements is not None and not isinstance(requirements, dict)):
        raise ValueError('unsupported configuration response')
    required = (requirements or {}).get('sqliteHome')
    root = (required if required is not None else
            os.environ.get('CODEX_SQLITE_HOME') or config.get('sqlite_home') or
            os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    if not isinstance(root, (str, Path)) or not str(root).strip():
        raise ValueError('invalid state directory')
    root = Path(root).expanduser()
    # Codex resolves configured paths. Refuse an unresolved relative directory
    # rather than silently reading relative to the bridge's working directory.
    if not root.is_absolute():
        raise ValueError('unresolved state directory')
    versions = {p.name for p in root.glob('state_*.sqlite')}
    if versions != {STATE_FILE}:
        raise ValueError('unsupported or missing native state version')
    return root / STATE_FILE


def require_owned_store(path):
    info, parent = path.lstat(), path.parent.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or
            info.st_mode & 0o022 or parent.st_mode & 0o022):
        raise ValueError('Codex native store is not owned and protected')


def _saved_thread_row(route, include_rollout=False, *, path=None):
    path = path if path is not None else state_path(route.get('executable'))
    require_owned_store(path)
    sid = str(uuid.UUID(route['session']))
    fields = 'cwd, archived, rollout_path' if include_rollout else 'cwd, archived'
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=LOCK_TIMEOUT)) as connection:
            row = connection.execute(f'SELECT {fields} FROM threads WHERE id=?', (sid,)).fetchone()
    except sqlite3.Error as error:
        raise ValueError('Codex native thread metadata unavailable') from error
    if row is None or not isinstance(row[0], str) or row[1] not in (0, 1):
        raise ValueError('Codex native thread identity or archive status unavailable')
    return row


def saved_thread(route):
    """Read exact workspace and archive status without changing the native store."""
    cwd, archived = _saved_thread_row(route)
    return cwd, bool(archived)


def verify_archive(route, *, path=None):
    """Require the native archive bit and the exact owned rollout in the archive."""
    try:
        cwd, archived, rollout_path = _saved_thread_row(route, include_rollout=True, path=path)
        if not Path(cwd).is_absolute() or Path(cwd).resolve() != Path(route['cwd']).resolve():
            raise ValueError('workspace differs')
        if not archived or not isinstance(rollout_path, str):
            raise ValueError('archive not recorded')
        rollout = Path(rollout_path)
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()
        info = rollout.lstat()
        if (not rollout.is_absolute() or rollout.resolve().parent != home / 'archived_sessions'
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size == 0):
            raise ValueError('archive rollout unavailable')
        with rollout.open('rb') as source:
            if not source.read(1):
                raise ValueError('archive rollout is empty')
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, TimeoutExpired):
        # Native errors may contain local paths or app-server diagnostics.
        raise ValueError('Native Codex archive could not be verified; closing route kept') from None
