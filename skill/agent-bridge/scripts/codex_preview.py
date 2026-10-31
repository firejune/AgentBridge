"""Best-effort sidebar preview for an already accepted, nonhuman bridge task.

Compatibility fallback for Codex's native state_5 store: the installed metadata
API has no preview setter. Never create/migrate a store or change thread identity.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

import sessions

CONFIG_TIMEOUT = 2
LOCK_TIMEOUT = .1
STATE_FILE = 'state_5.sqlite'


def unavailable(reason):
    return {'status': 'unavailable', 'warning': 'Codex sidebar preview unavailable: ' + reason}


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
    # rather than silently writing relative to the bridge's working directory.
    if not root.is_absolute():
        raise ValueError('unresolved state directory')
    versions = {p.name for p in root.glob('state_*.sqlite')}
    if versions != {STATE_FILE}:
        raise ValueError('unsupported or missing native state version')
    return root / STATE_FILE


def initialize(route, text):
    """Fill only an empty preview; errors never change delivery or cause replay.

    Concurrent accepted tasks race in SQLite, so the first successful CAS wins.
    The native writer may later replace the preview; no repair loop is installed.
    """
    try:
        thread = str(uuid.UUID(route['session']))
        if not isinstance(text, str) or not text.strip():
            return unavailable('empty task')
        path = state_path(route.get('executable'))
        info, parent = path.lstat(), path.parent.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or
                info.st_mode & 0o022 or parent.st_mode & 0o022):
            return unavailable('native store is not owned and protected')
        # mode=rw refuses a missing database; SQLite owns WAL/concurrency, and the
        # parameter-bound update never overwrites a populated native preview.
        with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                                     timeout=LOCK_TIMEOUT)) as connection, connection:
            columns = {r[1]: r for r in connection.execute('PRAGMA table_info(threads)')}
            expected = {'id': ('TEXT', 1), 'preview': ('TEXT', 0),
                        'cwd': ('TEXT', 0), 'first_user_message': ('TEXT', 0)}
            if any(name not in columns or columns[name][2].upper() != kind or
                   columns[name][5] != primary for name, (kind, primary) in expected.items()):
                return unavailable('unsupported native thread schema')
            row = connection.execute('SELECT preview, cwd FROM threads WHERE id=?', (thread,)).fetchone()
            if row is None:
                return unavailable('accepted thread absent from native store')
            if Path(row[1]).resolve() != Path(route['cwd']).resolve():
                return unavailable('native thread workspace differs')
            if row[0] != '':
                return {'status': 'preserved'}
            def preview_only(action, table, column, database, source):
                # Native INSERT and timestamp-column triggers do not fire for
                # this statement. Refuse any trigger writes or new side effects
                # without maintaining a SQL parser/allowlist of trigger bodies.
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                    allowed = (action == sqlite3.SQLITE_UPDATE and table == 'threads'
                               and column == 'preview' and database == 'main' and source is None)
                    return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(preview_only)
            changed = connection.execute("UPDATE threads SET preview=? WHERE id=? AND preview=''",
                                         (text, thread)).rowcount
        return {'status': 'initialized' if changed else 'preserved'}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, sessions.DiscoveryError):
        # Raw config/SQLite errors may contain private paths or task content.
        return unavailable('configuration or native store could not be updated; delivery remains accepted')
