"""Plan and verify native Codex archival without writing the native database."""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import time
import uuid

import codex_preview
import codex_queue
import codex_rollout
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
    def __init__(self, path, *, include_rollouts=False):
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
                self.rollouts = (dict(db.execute('SELECT id, rollout_path FROM threads'))
                                 if include_rollouts else {})
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


def preview_snapshot(path, *, include_rollouts=False):
    with codex_preview.private_store_copy(path) as copy:
        return Snapshot(copy, include_rollouts=include_rollouts)


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


def readback(path, before, sid, *, self_archive=False):
    after = preview_snapshot(path, include_rollouts=True) if self_archive else Snapshot(path)
    affected = before.descendants(sid) | after.descendants(sid) | {sid}
    if not affected <= after.rows.keys():
        raise ValueError('Thread metadata missing during archive readback')
    return after, affected


def native_handoff_digest(path, descendants):
    return digest({'store': str(path), 'descendants': sorted(descendants)})


class DescendantRefusal(ValueError):
    def __init__(self, reason, descendant, state):
        super().__init__(reason + '; no self-archive entry was submitted')
        self.fields = {'reason': reason, 'descendant': descendant, 'descendant_state': state}


def require_terminal(snapshot, sid, deadline=None):
    state, stamp = codex_rollout.turn_state(snapshot.rollouts.get(sid), sid, deadline)
    if state != 'terminal':
        raise DescendantRefusal('descendant_turn_' + state, sid, state)
    return stamp


def cascade_digest(path, route, snapshot, reviewed, deferred):
    members = [route['session'], *reviewed]
    return digest({'store': str(path), 'route': route_store.route_record(route),
                   'members': [(sid, snapshot.parents.get(sid), snapshot.rows[sid]['cwd']) for sid in members],
                   'deferred': deferred})


def self_archive_snapshot(route, reviewed, deferred, scope):
    path = sweep_store()
    try:
        snapshot = preview_snapshot(path, include_rollouts=True)
        root = route['session']
        descendants = snapshot.descendants(root)
        if (absolute_path(snapshot.rows[root]['cwd']) != absolute_path(route['cwd'])
                or not descendants <= snapshot.rows.keys()
                or (reviewed is not None and (reviewed != sorted(descendants)
                    or cascade_digest(path, route, snapshot, reviewed, deferred) != scope))
                or (reviewed is None and native_handoff_digest(path, descendants) != scope)):
            raise ValueError('scope mismatch')
    except NATIVE_ERRORS + (KeyError,):
        raise ValueError('Codex native close subtree or saved profile changed') from None
    return snapshot, descendants


def self_archive_scope(route, reviewed, deferred, scope, *, reserving=False):
    snapshot, descendants = self_archive_snapshot(route, reviewed, deferred, scope)
    unarchived = {sid for sid in descendants if not snapshot.rows[sid]['archived']}
    approved = set(deferred)
    if not unarchived <= approved or (reserving and unarchived != approved):
        raise ValueError('Codex native close has an unapproved unarchived descendant or the deferred set changed')
    return snapshot, unarchived


def self_archive_readback(route):
    reviewed = route.get('closing_reviewed_descendants')
    deferred = set(route.get('closing_cascade_deferred', ()))
    root = route['session']
    snapshot, descendants = self_archive_snapshot(route, reviewed, sorted(deferred), route['closing_native_scope'])
    members = [*(sorted(descendants) if reviewed is None else reviewed), root]
    verified = {sid: snapshot.rows[sid]['archived'] and codex_rollout.verify_archived(snapshot.rollouts[sid], sid)
                for sid in members}
    if reviewed is None:
        # Version 4/5 pending reservations have the original subtree digest.
        # Recover the bound membership and apply the same exact rollout proof.
        if not all(verified.values()):
            raise ValueError('Native Codex archive could not be verified; closing route kept')
        return None
    result = []
    cascade = deferred and route.get('closing_self_archive_enqueued')
    for sid in members:
        bit = snapshot.rows[sid]['archived']
        archived = verified[sid]
        if sid == root:
            method = 'root_cascade' if cascade else 'self_archive'
        elif sid in deferred:
            method = 'root_cascade' if cascade else 'deferred_native_readback'
        else:
            method = 'saved_or_previously_archived'
        result.append({'id': sid, 'archived': archived, 'method': method,
                       'status': 'archived' if archived else 'archive_unverified' if bit else 'not_archived'})
    return {'root_archived': result[-1]['archived'], 'results': result,
            'count': sum(row['archived'] for row in result)}


def check_descendant_queue(path, descendants):
    sid = descendants[0]
    try:
        with codex_preview.private_store_copy(path) as copy:
            with closing(sqlite3.connect(copy.as_uri() + '?mode=ro', uri=True,
                                         timeout=codex_preview.LOCK_TIMEOUT)) as db:
                db.execute('BEGIN')
                for sid in descendants:
                    if db.execute('SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1', (sid,)).fetchone():
                        raise DescendantRefusal('descendant_queue_pending', sid, 'pending')
    except DescendantRefusal:
        raise
    except (OSError, ValueError, sqlite3.Error):
        raise DescendantRefusal('descendant_queue_unknown', sid, 'unknown') from None


CASCADE_SETTLE_SECONDS = 1


def check_cascade_quiescence(route, deadline, *, clock=time.monotonic, sleep=None):
    reviewed = route['closing_reviewed_descendants']
    deferred = route['closing_cascade_deferred']
    scope = route['closing_native_scope']
    snapshot, unarchived = self_archive_scope(route, reviewed, deferred, scope)
    descendants = sorted(unarchived)
    if not descendants:
        return
    sid = descendants[0]
    try:
        state, queue = codex_queue.default_stores()
        if state != sweep_store():
            raise ValueError('store differs')
    except (OSError, ValueError):
        raise DescendantRefusal('descendant_queue_unknown', sid, 'unknown') from None
    check_descendant_queue(queue, descendants)
    before = {sid: require_terminal(snapshot, sid, deadline) for sid in descendants}
    if deadline - clock() <= CASCADE_SETTLE_SECONDS:
        raise ValueError('Codex self-archive deadline would expire during the descendant settle wait')
    (sleep or time.sleep)(CASCADE_SETTLE_SECONDS)
    if clock() >= deadline:
        raise ValueError('Codex self-archive deadline expired during the descendant settle wait')
    latest, still_unarchived = self_archive_scope(route, reviewed, deferred, scope)
    for sid in descendants:
        if (sid not in still_unarchived or require_terminal(latest, sid, deadline) != before[sid]):
            raise DescendantRefusal('descendant_turn_unknown', sid, 'changed')
    check_descendant_queue(queue, descendants)
    # Accepted residual races: input after these checks, a dispatched start still
    # absent from the rollout after the settle interval, and the time before
    # Desktop runs the root entry. The caller enqueues immediately after this.


def native_handoff_snapshot(route):
    """Check the saved Desktop subtree before delegating a root-only native archive."""
    try:
        path = sweep_store()
        snapshot = preview_snapshot(path) if route.get('closing_self_archive') else Snapshot(path)
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


def check_native_operator_processes(route, expected_scope, *, cascade=None):
    """Recheck process CWDs in the claiming CLI, outside the broker's serialized mutations."""
    try:
        _, holders = process_cwds(exempt_desktop=True)
    except NATIVE_ERRORS:
        raise ValueError('Codex native close could not verify process working directories. Closing route kept.') from None
    try:
        if cascade is None:
            snapshot, descendants, scope = native_handoff_snapshot(route)
            if scope != expected_scope:
                raise ValueError('Codex native close subtree or saved profile changed')
        else:
            snapshot, descendants = self_archive_snapshot(
                route, cascade['reviewed'], cascade['deferred'], expected_scope)
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


def archive_group(binary, path, directory, prefix, item, timeout, close_route=None, own=None, *,
                  defer_native_root=False, self_archive=False):
    root = item['id']
    expected = {row['id']: row for row in [item, *item['members']]}
    subtrees = {sid: set(row['descendants']) for sid, row in expected.items()}
    try:
        read_snapshot = preview_snapshot if self_archive else Snapshot
        initial = read_snapshot(path)
        if not expected.keys() <= initial.rows.keys() or any(
                initial.descendants(sid) != descendants for sid, descendants in subtrees.items()):
            return [{'id': root, 'status': 'descendants_changed'}], False
    except NATIVE_ERRORS:
        return [{'id': root, 'status': 'readback_unavailable'}], True
    receipts, failed, deferred = [], set(), set()
    for sid in sorted(expected, key=lambda key: (len(subtrees[key]), key)):
        check = {}
        cascade_ancestor = bool(subtrees[sid] & deferred)

        def before_call():
            guard = Protections(directory, prefix, close_route, own)
            before = read_snapshot(path, include_rollouts=self_archive)
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
            if any(not before.rows[key]['archived'] for key in subtrees[sid] - deferred):
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
            if cascade_ancestor:
                # A saved archive here would recursively reach the held worker.
                # Only the reviewed root may run that cascade.
                before = before_call()
                if before is not None:
                    require_terminal(before, sid)
                    deferred.add(sid)
                    check['status'] = 'deferred_cascade_ancestor'
                receipts.append({**receipt, **check})
                if check['status'] in ('already_archived', 'deferred_cascade_ancestor'):
                    continue
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
            after, affected = readback(path, before, sid, self_archive=self_archive)
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
            if self_archive and error:
                if (receipt['status'] == 'not_archived' and not row['archived'] and not missing and not late
                        and error == {'kind': 'rpc_rejected', 'code': -32600, 'active_writer': True}
                        and after.descendants(sid) == subtrees[sid]
                        and after.descendants(root) == subtrees[root]
                        and before.rollouts[sid] == after.rollouts[sid]):
                    require_terminal(after, sid)
                    receipt['status'] = 'deferred_active_writer'
                    deferred.add(sid)
                elif receipt['status'] == 'archived':
                    receipt['status'] = 'provider_archive_unconfirmed'
        except DescendantRefusal as error:
            receipt.update(status='refused', **error.fields)
            receipts.append(receipt)
            failed.add(sid)
            continue
        except NATIVE_ERRORS:
            receipt['status'] = 'restore_failed' if 'unlisted' in receipt else 'readback_unavailable'
        receipts.append(receipt)
        if receipt['status'] in ('restore_failed', 'readback_unavailable', 'provider_shutdown_unconfirmed'):
            return receipts, True
        if receipt['status'] in ('not_archived', 'provider_archive_unconfirmed'):
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
    snapshot = preview_snapshot(path) if prefer_native is not None else Snapshot(path)
    sid = thread_id(route['session'])
    row = snapshot.rows.get(sid)
    if row is None or absolute_path(row['cwd']) != absolute_path(route['cwd']):
        raise ValueError('Codex native thread workspace or identity does not match the route')
    guard = Protections(directory, close_route=route)
    item = group(snapshot, guard, sid, 'explicit_close')
    if item['refusals']:
        results, stopped = [{'id': sid, 'status': 'refused', 'refusals': item['refusals']}], False
    else:
        preference = prefer_native() if prefer_native is not None else False
        defer_native_root = bool(preference)
        if defer_native_root and path != sweep_store():
            raise ValueError('The enrolled close operator requires the saved Desktop profile')
        results, stopped = archive_group(binary, path, directory, None, item, timeout, route, guard.own,
                                         defer_native_root=defer_native_root, self_archive=preference == 'self_archive')
    final = next((result for result in results if result['id'] == sid), {})
    archived = final.get('archived') is True or final.get('status') == 'already_archived'
    note = 'Native Codex archive was not verified. Closing route kept.'
    if archived:
        # The bit alone is not an archive: Codex moves the rollout when it archives natively (#212).
        try:
            codex_preview.verify_archive(route, path=path)
        except ValueError:
            archived = False
            final['status'] = 'not_archived'
            final['reason'] = 'rollout_not_archived'
            note = 'Codex archive not verified: the rollout is not under archived_sessions. Closing route kept.'
        else:
            note = 'Native Codex archive verified: archive bit and rollout under archived_sessions.'
    elif final.get('reason') == 'desktop_holds_thread':
        note = 'Codex Desktop holds this thread; it was not archived and the route is kept.'
    elif final.get('status') == 'descendant_not_archived' and any(
            result.get('reason') == 'desktop_holds_thread' and result.get('archived') is False for result in results):
        note = 'Codex Desktop holds an unarchived descendant. The parent was not archived and the route is kept.'
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
        if preference == 'self_archive':
            held = sorted(row['id'] for row in results if row['status'] == 'deferred_active_writer')
            deferred = sorted(held + [row['id'] for row in results if row['status'] == 'deferred_cascade_ancestor'])
            result.update(cascade={'reviewed': item['descendants'], 'deferred': deferred, 'held': held},
                          native_scope=cascade_digest(path, route, snapshot, item['descendants'], deferred),
                          note='The root self-archive covers the reviewed Codex subtree.')
    elif (refusal := next((row for row in results if 'descendant_state' in row), None)):
        result.update(status='not_archived', archive_path='not_archived')
        result.update({key: refusal[key] for key in ('reason', 'descendant', 'descendant_state')})
    return result
