"""Plan and verify native Codex archival without writing the native database."""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

import codex_preview
import codex_queue
import route_store
import sessions
import wt

MIN_AGE = 24 * 60 * 60
NATIVE_ERRORS = (ValueError, OSError, subprocess.TimeoutExpired)


def executable():
    for path in (codex_queue.CLI, Path('/Applications/Codex.app/Contents/Resources/codex')):
        if os.access(path, os.X_OK):
            return str(path)
    raise ValueError('Bundled Codex executable unavailable')


def sweep_store():
    root = Path(os.environ.get('CODEX_SQLITE_HOME') or os.environ.get('CODEX_HOME')
                or Path.home() / '.codex').expanduser()
    if not root.is_absolute():
        raise ValueError('Codex state directory must be absolute')
    return absolute_path(root) / codex_preview.STATE_FILE


def thread_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid Codex thread UUID')
    return value


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('Expected an absolute path')
    try:
        return path.resolve()
    except RuntimeError:
        raise ValueError('Could not resolve workspace path') from None


class Snapshot:
    def __init__(self, path):
        codex_preview.require_owned_store(path)
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                         timeout=codex_preview.LOCK_TIMEOUT)) as db:
                db.execute('BEGIN')
                columns = {row[1] for row in db.execute('PRAGMA table_info(threads)')}
                activity = ('MAX(updated_at, COALESCE(updated_at_ms / 1000.0, updated_at))'
                            if 'updated_at_ms' in columns else 'updated_at')
                rows = db.execute(f'SELECT id, title, cwd, {activity}, archived, archived_at FROM threads').fetchall()
                edges = db.execute('SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges').fetchall()
        except sqlite3.Error:
            raise ValueError('Codex thread or spawn-edge metadata unavailable') from None
        self.rows, self.parents, self.children = {}, {}, {}
        for sid, title, cwd, activity, archived, archived_at in rows:
            if (not isinstance(title, str) or not isinstance(cwd, str)
                    or not Path(cwd).is_absolute() or not isinstance(activity, (int, float))
                    or not math.isfinite(activity) or archived not in (0, 1)):
                raise ValueError('Invalid Codex native thread metadata')
            self.rows[thread_id(sid)] = dict(id=sid, title=title, cwd=cwd, activity=activity,
                                            archived=bool(archived), archived_at=archived_at)
        for parent, child in edges:
            thread_id(parent)
            thread_id(child)
            if child in self.parents:
                raise ValueError('Ambiguous Codex spawn parent')
            self.parents[child] = parent
            self.children.setdefault(parent, []).append(child)

    def descendants(self, root):
        pending = list(self.children.get(root, ()))
        seen = {root}
        while pending:
            sid = pending.pop()
            if sid in seen:
                raise ValueError('Cycle in Codex spawn edges')
            seen.add(sid)
            pending.extend(self.children.get(sid, ()))
        return seen - {root}

    def has_archived_parent(self, sid):
        seen = {sid}
        while sid in self.parents:
            sid = self.parents[sid]
            if sid in seen:
                raise ValueError('Cycle in Codex spawn edges')
            seen.add(sid)
            if self.rows.get(sid, {}).get('archived'):
                return True
        return False


def preview_snapshot(path):
    """Copy a stable DB/WAL pair so preview cannot create native SQLite sidecars."""
    codex_preview.require_owned_store(path)
    wal = path.with_name(path.name + '-wal')
    journal = path.with_name(path.name + '-journal')

    def stamp(file):
        try:
            info = file.lstat()
        except FileNotFoundError:
            return None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise ValueError('Codex preview source is not owned and protected')
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    before = (stamp(path), stamp(wal), stamp(journal))
    if before[0] is None or before[2] is not None:
        raise ValueError('Codex preview source missing or rollback journal present')
    with tempfile.TemporaryDirectory(prefix='agent-bridge-archive-') as directory:
        copy = Path(directory) / path.name
        copy.touch(mode=0o600)
        shutil.copyfile(path, copy)
        if before[1] is not None:
            wal_copy = copy.with_name(wal.name)
            wal_copy.touch(mode=0o600)
            shutil.copyfile(wal, wal_copy)
        if before != (stamp(path), stamp(wal), stamp(journal)):
            raise ValueError('Codex state changed during preview; manifest not generated')
        return Snapshot(copy)


DESKTOP_HELPER = codex_queue.APP / 'Contents/Resources/cua_node/bin/node_repl'


def desktop_helpers(rows):
    """PIDs among lsof rows that are Desktop's per-thread helpers: the bundled binary run by the queue owner.

    Anything unverified is not exempt: no single Desktop owner, another parent, another binary.
    """
    candidates = sorted({pid for pid, command, _ in rows if command == DESKTOP_HELPER.name})
    if not candidates:
        return set()
    read = codex_queue.checked_output
    try:
        owner = codex_queue.desktop_queue_owner(codex_queue.default_stores()[1], read)
        if owner is None:
            return set()
        listing = read(['/bin/ps', '-ww', '-o', 'pid=,ppid=,uid=,command=', '-p', ','.join(map(str, candidates))])
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    helpers = set()
    for line in listing.splitlines():
        fields = line.split(None, 3)
        if (len(fields) == 4 and fields[1] == owner[0] and fields[2] == str(os.getuid())
                and fields[3] == str(DESKTOP_HELPER)):
            helpers.add(int(fields[0]))
    return helpers


def process_cwds(own=None, *, exempt_desktop=False):
    rows = wt.open_files(skip=(os.getpid(),), cwd_only=True)
    if own is None:
        own = desktop_helpers(rows) if exempt_desktop else set()
    own = frozenset(own)
    return own, {absolute_path(cwd) for pid, _, cwd in rows if pid not in own}


class Protections:
    def __init__(self, directory, prefix=None, close_route=None, own=None):
        snapshot = route_store.read_snapshot(directory / 'routes.json')
        self.protected = set()
        endpoints, channels = [], []
        if snapshot is not None:
            endpoints = [route_store.endpoint_record(item) for item in snapshot['endpoints']]
            channels = [route_store.channel_record(item) for item in snapshot['channels']]
        if close_route is not None and close_route not in endpoints:
            raise ValueError('Closing route differs from routing snapshot; check --state-dir')
        for route in endpoints + [channel['route'] for channel in channels if channel['active']]:
            if route['transport'] == 'codex' and route != close_route:
                self.protected.add(route['session'])
        # An explicit close names the thread, so the helpers Desktop itself runs in that
        # thread's folder do not make it "in use" (issue #175). The sweep exempts nothing.
        # They are identified once, before the archive client starts: that client opens the
        # queue store too, and Desktop is then no longer its only owner. Later checks reuse the set.
        self.own, self.holders = process_cwds(own, exempt_desktop=close_route is not None)
        self.cutoff = None if close_route is not None else time.time() - MIN_AGE
        self.prefix = prefix

    def reason(self, row):
        if row is None:
            return 'missing_thread'
        cwd = absolute_path(row['cwd'])
        if self.prefix is not None and not cwd.is_relative_to(self.prefix):
            return 'outside_cwd_prefix'
        if row['id'] in self.protected:
            return 'registered_route'
        if any(holder.is_relative_to(cwd) for holder in self.holders):
            return 'running_process_cwd'
        if self.cutoff is not None and not row['archived'] and row['activity'] > self.cutoff:
            return 'recent_activity'
        return None


def root_reason(snapshot, row):
    try:
        Path(row['cwd']).stat()
    except (FileNotFoundError, NotADirectoryError):
        return 'missing_cwd'
    if snapshot.has_archived_parent(row['id']):
        return 'archived_parent'
    return None


def describe(row, reason):
    return {key: row[key] for key in ('id', 'title', 'cwd', 'activity')} | {'reason': reason}


def group(snapshot, guard, root, reason):
    descendants = snapshot.descendants(root)
    refused = [{'id': sid, 'reason': why} for sid in sorted(descendants | {root})
               if (why := guard.reason(snapshot.rows.get(sid)))]
    return {**describe(snapshot.rows[root], reason), 'descendants': sorted(descendants),
            'members': [{**describe(snapshot.rows[sid], 'descendant'),
                         'descendants': sorted(snapshot.descendants(sid))}
                        for sid in sorted(descendants) if sid in snapshot.rows],
            'refusals': refused}


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def plan(path, directory, prefix):
    snapshot = preview_snapshot(path)
    guard = Protections(directory, prefix)
    candidates, refusals = {}, []
    for sid, row in sorted(snapshot.rows.items()):
        if row['archived'] or (prefix is not None and not absolute_path(row['cwd']).is_relative_to(prefix)):
            continue
        reason = guard.reason(row)
        qualification = None if reason else root_reason(snapshot, row)
        if qualification:
            item = group(snapshot, guard, sid, qualification)
            if item['refusals']:
                refusals.append(item)
            else:
                candidates[sid] = item
        else:
            refusals.append(describe(row, reason or 'existing_cwd_without_archived_parent'))
    covered = {child for item in candidates.values() for child in item['descendants']}
    selected = [item for sid, item in candidates.items() if sid not in covered]
    allowed = {child for item in selected for child in item['descendants']}
    body = {'version': 1, 'store': str(path), 'routing_directory': str(directory),
            'cwd_prefix': str(prefix) if prefix is not None else None,
            'candidates': selected, 'refusals': [item for item in refusals if item['id'] not in allowed]}
    return {**body, 'digest': digest(body)}


def reviewed(path, directory, prefix, manifest):
    if not isinstance(manifest, dict):
        raise ValueError('Invalid archive manifest')
    body = {key: value for key, value in manifest.items() if key != 'digest'}
    if manifest.get('digest') != digest(body):
        raise ValueError('Archive manifest digest mismatch')
    scope = body.get('cwd_prefix')
    if scope is not None:
        scope = absolute_path(scope)
    if (body.get('version') != 1 or body.get('store') != str(path)
            or body.get('routing_directory') != str(directory)
            or (prefix is not None and prefix != scope)):
        raise ValueError('Archive manifest store or scope mismatch')
    items = body['candidates']
    ids = set()
    for item in items:
        members = [thread_id(row['id']) for row in item['members']]
        item_ids = {thread_id(item['id']), *members}
        if (len(item_ids) != len(members) + 1 or sorted(members) != item['descendants']
                or item['refusals'] != [] or ids & item_ids):
            raise ValueError('Invalid or overlapping archive manifest candidates')
        for row in item['members']:
            subtree = row['descendants']
            if (subtree != sorted(set(subtree)) or row['id'] in subtree
                    or not set(subtree) <= set(members)):
                raise ValueError('Invalid descendant set in archive manifest')
        ids.update(item_ids)
    return items, scope


def invoke(binary, timeout, method, sid, before_call=None):
    """One saved app-server invocation. Stop it before interpreting readback."""
    before, attempted, error = None, False, None
    try:
        with sessions.CodexClient(executable=binary, mode='saved', timeout=timeout) as client:
            before = before_call() if before_call else None
            if before_call and before is None:
                return None, None
            attempted = True
            try:
                client.call(method, {'threadId': sid})
            except sessions.CodexRpcError as rejection:
                error = {'kind': 'rpc_rejected', 'code': rejection.code if type(rejection.code) is int else None}
                if (rejection.code, rejection.message) == (-32600, f'thread {sid} already has an active writer'):
                    error['active_writer'] = True
            except NATIVE_ERRORS:
                error = {'kind': 'rpc_uncertain'}
    except NATIVE_ERRORS:
        if not attempted:
            raise ValueError('Native Codex app-server startup or safety check failed') from None
        error = {**(error or {'kind': 'rpc_uncertain'}), 'shutdown_unconfirmed': True}
    return before, error


def readback(path, before, sid):
    after = Snapshot(path)
    affected = before.descendants(sid) | after.descendants(sid) | {sid}
    if not affected <= after.rows.keys():
        raise ValueError('Thread metadata missing during archive readback')
    return after, affected


def native_handoff_digest(path, descendants):
    return digest({'store': str(path), 'descendants': sorted(descendants)})


def native_handoff_snapshot(route):
    """Check the saved Desktop subtree before delegating a root-only native archive."""
    try:
        path = sweep_store()
        snapshot = Snapshot(path)
        root = route['session']
        row = snapshot.rows[root]
        if absolute_path(row['cwd']) != absolute_path(route['cwd']):
            raise ValueError('workspace mismatch')
        descendants = snapshot.descendants(root)
        if not descendants <= snapshot.rows.keys():
            raise ValueError('descendant missing')
    except NATIVE_ERRORS + (KeyError,):
        raise ValueError('Codex native close subtree could not be verified') from None
    if any(not snapshot.rows[sid]['archived'] for sid in descendants):
        raise ValueError('Codex native close has an unarchived descendant')
    return snapshot, descendants, native_handoff_digest(path, descendants)


def native_handoff_scope(route):
    _, descendants, scope = native_handoff_snapshot(route)
    return descendants, scope


def check_native_operator_processes(route, expected_scope):
    """Recheck process CWDs in the claiming CLI, outside the broker's serialized mutations."""
    try:
        _, holders = process_cwds(exempt_desktop=True)
    except NATIVE_ERRORS:
        raise ValueError('Codex native close could not verify process working directories. Closing route kept.') from None
    try:
        snapshot, descendants, scope = native_handoff_snapshot(route)
        if scope != expected_scope:
            raise ValueError('Codex native close subtree or saved profile changed')
        workspaces = {absolute_path(snapshot.rows[sid]['cwd']) for sid in descendants | {route['session']}}
        if any(holder.is_relative_to(cwd) for holder in holders for cwd in workspaces):
            raise ValueError('A running process uses the Codex native close workspace')
    except ValueError as error:
        raise ValueError(f'{error}. Closing route kept.') from None
    except OSError:
        raise ValueError('Codex native close workspaces could not be verified. Closing route kept.') from None


def restore_unlisted(binary, path, timeout, after, late):
    restored, errors = [], []
    while late:
        # Restore the unlisted ancestor if its unlisted children are also targets.
        sid = min(late, key=lambda candidate: (-len(after.descendants(candidate)), candidate))
        targets = late & (after.descendants(sid) | {sid})
        try:
            _, error = invoke(binary, timeout, 'thread/unarchive', sid)
        except NATIVE_ERRORS:
            errors.append({'id': sid, 'kind': 'startup_failed'})
            return after, restored, errors, {'unrestored': sorted(late)}
        if error:
            errors.append({'id': sid, **error})
        try:
            current, affected = readback(path, after, sid)
        except NATIVE_ERRORS:
            return after, restored, errors, {'unrestored': sorted(late), 'readback': 'unavailable'}
        collateral = sorted(key for key in affected - targets
                            if key not in after.rows or current.rows[key]['archived'] != after.rows[key]['archived'])
        confirmed = {key for key in targets if not current.rows[key]['archived']}
        restored.extend(sorted(confirmed))
        if collateral or confirmed != targets or (error or {}).get('shutdown_unconfirmed'):
            return current, restored, errors, {'unrestored': sorted(late - confirmed), 'collateral': collateral}
        late -= confirmed
        after = current
    return after, restored, errors, None


def archive_group(binary, path, directory, prefix, item, timeout, close_route=None, own=None, *, defer_native_root=False):
    root = item['id']
    expected = {row['id']: row for row in [item, *item['members']]}
    subtrees = {sid: set(row['descendants']) for sid, row in expected.items()}
    try:
        initial = Snapshot(path)
        if not expected.keys() <= initial.rows.keys() or any(
                initial.descendants(sid) != descendants for sid, descendants in subtrees.items()):
            return [{'id': root, 'status': 'descendants_changed'}], False
    except NATIVE_ERRORS:
        return [{'id': root, 'status': 'readback_unavailable'}], True
    receipts, failed = [], set()
    for sid in sorted(expected, key=lambda key: (len(subtrees[key]), key)):
        check = {}

        def before_call():
            guard = Protections(directory, prefix, close_route, own)
            before = Snapshot(path)
            if (before.descendants(root) != subtrees[root]
                    or before.descendants(sid) != subtrees[sid]):
                check['status'] = 'descendants_changed'
                return None
            for key in sorted(expected):
                row = before.rows.get(key)
                why = guard.reason(row)
                if why or row['cwd'] != expected[key]['cwd']:
                    check.update(status='refused', blocked=key, reason=why or 'workspace_changed')
                    return None
            if not close_route and not root_reason(before, before.rows[root]):
                check.update(status='refused', blocked=root, reason='existing_cwd_without_archived_parent')
                return None
            if before.rows[sid]['archived'] and (sid != root or not defer_native_root):
                check.update(status='already_archived', archived_at=before.rows[sid]['archived_at'])
                return None
            if any(not before.rows[key]['archived'] for key in subtrees[sid]):
                check['status'] = 'descendant_not_archived'
                return None
            return before

        if subtrees[sid] & failed:
            receipts.append({'id': sid, 'status': 'descendant_not_archived'})
            failed.add(sid)
            continue
        receipt = {'id': sid}
        try:
            if sid == root and defer_native_root:
                # Keep the same final review without starting a saved app-server for the root.
                # A saved archive can succeed while leaving Desktop's active list stale.
                before = before_call()
                if before is not None:
                    check['status'] = 'awaiting_native_archive'
                receipts.append({**receipt, **check})
                return receipts, False
            before, error = invoke(binary, timeout, 'thread/archive', sid, before_call)
            if before is None:
                receipts.append({**receipt, **check})
                if check['status'] == 'already_archived':
                    continue
                return receipts, False
            receipt['provider_error'] = error
            if close_route is not None and (error or {}).get('active_writer'):
                # Desktop holds the thread's writer. Agent Bridge never writes Codex state (#212):
                # the thread stays open and the route is kept until Codex archives it natively.
                receipt['reason'] = 'desktop_holds_thread'
            after, affected = readback(path, before, sid)
            late = {key for key in affected - subtrees[sid] - {sid} if after.rows[key]['archived']
                    and not before.rows.get(key, {}).get('archived')}
            if late:
                receipt['unlisted'] = sorted(late)
                after, restored, errors, failure = restore_unlisted(binary, path, timeout, after, late)
                receipt.update(restored_unlisted=restored, restore_provider_errors=errors)
                if failure:
                    receipt.update(status='restore_failed', **failure)
                    if failure.get('readback') == 'unavailable':
                        receipt['archive_readback'] = after.rows[sid]['archived']
                        receipts.append(receipt)
                        return receipts, True
            row = after.rows[sid]
            receipt.update(archived=row['archived'], archived_at=row['archived_at'])
            if row['archived']:
                receipt['undo'] = shlex.join([binary, 'unarchive', sid])
            missing = sorted(key for key in affected - {sid} - set(receipt.get('restored_unlisted', ()))
                             if not after.rows[key]['archived'])
            if missing:
                receipt['not_archived_descendants'] = missing
            if (error or {}).get('shutdown_unconfirmed'):
                receipt.setdefault('status', 'provider_shutdown_unconfirmed')
            receipt.setdefault('status', 'archived' if row['archived'] and not missing else 'not_archived')
        except NATIVE_ERRORS:
            receipt['status'] = 'restore_failed' if 'unlisted' in receipt else 'readback_unavailable'
        receipts.append(receipt)
        if receipt['status'] in ('restore_failed', 'readback_unavailable', 'provider_shutdown_unconfirmed'):
            return receipts, True
        if receipt['status'] == 'not_archived':
            failed.add(sid)
    return receipts, False


def apply(path, directory, prefix, manifest, timeout):
    items, scope = reviewed(path, directory, prefix, manifest)
    receipts = []
    stopped = False
    if items:
        binary = executable()
        if codex_preview.state_path(binary).resolve() != path:
            raise ValueError('Bundled Codex state directory differs from reviewed store')
        for item in items:
            rows, stopped = archive_group(binary, path, directory, scope, item, timeout)
            receipts.extend(rows)
            if stopped:
                break
    return {'digest': manifest['digest'], 'results': receipts, 'stopped': stopped,
            'count': sum(row.get('archived') is True for row in receipts),
            'complete': all(row['status'] in ('archived', 'already_archived') for row in receipts)}


def close(route, directory, timeout, *, prefer_native=None):
    route = route_store.endpoint_record(route)
    binary = executable()
    path = codex_preview.state_path(binary).resolve()
    snapshot = Snapshot(path)
    sid = thread_id(route['session'])
    row = snapshot.rows.get(sid)
    if row is None or absolute_path(row['cwd']) != absolute_path(route['cwd']):
        raise ValueError('Codex native thread workspace or identity does not match the route')
    guard = Protections(directory, close_route=route)
    item = group(snapshot, guard, sid, 'explicit_close')
    if item['refusals']:
        results, stopped = [{'id': sid, 'status': 'refused', 'refusals': item['refusals']}], False
    else:
        defer_native_root = prefer_native is not None and prefer_native()
        if defer_native_root and path != sweep_store():
            raise ValueError('The enrolled close operator requires the saved Desktop profile')
        results, stopped = archive_group(binary, path, directory, None, item, timeout, route, guard.own,
                                         defer_native_root=defer_native_root)
    final = next((result for result in results if result['id'] == sid), {})
    archived = final.get('archived') is True or final.get('status') == 'already_archived'
    note = 'Native Codex archive verified: archive bit and rollout under archived_sessions.'
    if archived:
        # The bit alone is not an archive: Codex moves the rollout when it archives natively (#212).
        try:
            codex_preview.verify_archive(route, path=path)
        except ValueError:
            archived = False
            final['status'] = 'not_archived'
            final['reason'] = 'rollout_not_archived'
            note = 'Codex archive not verified: the rollout is not under archived_sessions. Closing route kept.'
    elif final.get('reason') == 'desktop_holds_thread':
        note = 'Codex Desktop holds this thread; it was not archived and the route is kept.'
    incomplete = any(result['status'] not in ('archived', 'already_archived') for result in results)
    result = {'closed': archived, 'method': 'archive', 'archived': archived,
            'session': sid, 'host': 'codex', 'archived_at': final.get('archived_at', row['archived_at']),
            'resumable': True, 'note': note,
            'results': results, 'stopped': stopped, 'archive_incomplete': incomplete,
            'count': sum(result.get('archived') is True for result in results)}
    if final.get('status') == 'awaiting_native_archive':
        result.update(status='awaiting_native_archive',
                      native_scope=native_handoff_digest(path, item['descendants']),
                      note='The enrolled Codex Desktop operator archives the root.')
    return result
