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


def _resolve_archive_state_path(executable=None):
    try:
        return state_path(executable)
    except (OSError, ValueError, TypeError, KeyError, sessions.DiscoveryError):
        root = Path(
            os.environ.get('CODEX_SQLITE_HOME')
            or os.environ.get('CODEX_HOME')
            or Path.home() / '.codex'
        ).expanduser()
        candidate = root / STATE_FILE
        if candidate.is_file():
            return candidate
        raise ValueError(f'Codex state database is not found at {candidate}') from None


def require_owned_store(path):
    info, parent = path.lstat(), path.parent.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or
            info.st_mode & 0o022 or parent.st_mode & 0o022):
        raise ValueError('Codex native store is not owned and protected')


def saved_thread(route):
    """Read exact workspace and archive status without changing the native store."""
    path = state_path(route.get('executable'))
    require_owned_store(path)
    sid = str(uuid.UUID(route['session']))
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=LOCK_TIMEOUT)) as connection:
            row = connection.execute('SELECT cwd, archived FROM threads WHERE id=?', (sid,)).fetchone()
    except sqlite3.Error as error:
        raise ValueError('Codex native thread metadata unavailable') from error
    if row is None or not isinstance(row[0], str) or row[1] not in (0, 1):
        raise ValueError('Codex native thread identity or archive status unavailable')
    return row[0], bool(row[1])


def archive_thread(route, timeout=10):
    """Archive a Codex thread in state_5.sqlite and notify Codex Desktop over IPC."""
    import codex_live

    thread = str(uuid.UUID(route['session']))
    path = _resolve_archive_state_path(route.get('executable'))
    require_owned_store(path)
    archived_at = int(time.time())
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                                 timeout=max(LOCK_TIMEOUT, min(timeout, 5.0)))) as connection, connection:
        row = connection.execute('SELECT cwd FROM threads WHERE id=?', (thread,)).fetchone()
        if row is None:
            raise ValueError(f'Codex thread {thread} is absent from state_5.sqlite')
        row_cwd = row[0] or route.get('cwd') or ''
        connection.execute(
            'UPDATE threads SET archived=1, archived_at=? WHERE id=?',
            (archived_at, thread),
        )
        verify = connection.execute('SELECT archived, archived_at FROM threads WHERE id=?', (thread,)).fetchone()
        if not verify or int(verify[0]) != 1:
            raise ValueError(f'Codex thread {thread} did not read back as archived in state_5.sqlite')

    try:
        with codex_live.Client(timeout=min(timeout, 2.0)) as client:
            client.write({
                'type': 'broadcast',
                'method': 'thread-archived',
                'sourceClientId': client.client_id,
                'version': 2,
                'params': {'hostId': 'local', 'conversationId': thread, 'cwd': row_cwd},
            })
    except (codex_live.DeliveryError, OSError):
        pass

    return {
        'closed': True,
        'method': 'archive',
        'archived': True,
        'session': thread,
        'host': 'codex',
        'archived_at': archived_at,
        'resumable': True,
        'note': 'Archived in state_5.sqlite and notified Codex Desktop.',
    }
