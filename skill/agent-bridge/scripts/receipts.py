"""Client-side run capture and bounded, receipt-backed handovers (issue #141)."""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import stat
import subprocess
import sys
import termios
import uuid

import retros

SCHEMA = 'agent-bridge-run/1'
ID = re.compile(r'[0-9a-f]{32}')
SHA = re.compile(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}')


def now():
    return datetime.now(timezone.utc).isoformat()


def write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError('RUN_WRITE_FAILED')
        view = view[count:]


def git_snapshot(cwd):
    """Metadata only; failure stays unknown, never a clean-tree assertion."""
    try:
        head = subprocess.run(['git', '-C', str(cwd), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, timeout=5)
        if head.returncode or not SHA.fullmatch(head.stdout.strip()):
            return {'head': None, 'dirty': None}
        status = subprocess.run(['git', '-C', str(cwd), 'status', '--porcelain', '--untracked-files=normal'],
                                capture_output=True, timeout=5)
        return {'head': head.stdout.strip(), 'dirty': bool(status.stdout) if status.returncode == 0 else None}
    except (OSError, subprocess.TimeoutExpired):
        return {'head': None, 'dirty': None}


@contextmanager
def forward_signals(send):
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, lambda sig, frame: send(sig))
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def kill_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def same_terminal(first, second):
    return os.isatty(first) and os.isatty(second) and os.ttyname(first) == os.ttyname(second)


def routed_capture(argv, cwd, emit):
    """Inherit stdin; retain each output's route and tty detection independently.

    Outputs on the same terminal share one PTY. Independent pipes/PTYS are
    logged in observed read order, which cannot recover global write order.
    """
    readers, slaves, destinations, resizes = {}, [], {}, {}
    child = None
    separate_session = not os.isatty(sys.stdin.fileno())
    old_resize = signal.getsignal(signal.SIGWINCH)
    try:
        for target in (sys.stdout.fileno(), sys.stderr.fileno()):
            if target == sys.stderr.fileno() and same_terminal(sys.stdout.fileno(), target):
                destinations[target] = destinations[sys.stdout.fileno()]
                continue
            if os.isatty(target):
                reader, writer = pty.openpty()
                readers[reader] = target
                slaves.append(writer)
                attrs = termios.tcgetattr(target)
                # The destination terminal applies output processing once. Do
                # not translate LF twice via the intermediate output-only PTY.
                attrs[1] &= ~termios.OPOST
                termios.tcsetattr(writer, termios.TCSANOW, attrs)
                size = fcntl.ioctl(target, termios.TIOCGWINSZ, bytes(8))
                fcntl.ioctl(writer, termios.TIOCSWINSZ, size)
                resizes[reader] = target
            else:
                reader, writer = os.pipe()
                readers[reader] = target
                slaves.append(writer)
            destinations[target] = writer
        def resize(sig, frame):
            for reader, target in resizes.items():
                fcntl.ioctl(reader, termios.TIOCSWINSZ,
                            fcntl.ioctl(target, termios.TIOCGWINSZ, bytes(8)))
        if resizes:
            signal.signal(signal.SIGWINCH, resize)
        try:
            child = subprocess.Popen(argv, cwd=cwd, stdin=None,
                                     stdout=destinations[sys.stdout.fileno()],
                                     stderr=destinations[sys.stderr.fileno()],
                                     start_new_session=separate_session)
        except OSError as error:
            emit((f'{argv[0]}: {error}\n').encode(), sys.stderr.fileno())
            return 127 if isinstance(error, FileNotFoundError) else 126
        for writer in slaves:
            os.close(writer)
        slaves.clear()
        def send(sig):
            if separate_session:
                kill_group(child.pid, sig)
            elif sig != signal.SIGINT:
                # TTY stdin keeps the foreground process group: terminal
                # SIGINT already reaches the child. Suppress our interrupt
                # without delivering that same signal a second time.
                child.send_signal(sig)
        with forward_signals(send):
            while readers:
                ready, _, _ = select.select(list(readers), [], [])
                for reader in ready:
                    try:
                        block = os.read(reader, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO or reader not in resizes:
                            raise
                        block = b''
                    if not block:
                        del readers[reader]
                        resizes.pop(reader, None)
                        os.close(reader)
                    else:
                        emit(block, readers[reader])
            code = child.wait()
        return 128 - code if code < 0 else code
    finally:
        signal.signal(signal.SIGWINCH, old_resize)
        for fd in [*readers, *slaves]:
            os.close(fd)
        if child is not None and child.poll() is None:
            if separate_session:
                kill_group(child.pid, signal.SIGKILL)
            else:
                child.kill()
            child.wait()


def run(state, label, cwd, argv):
    if argv[:1] == ['--']:
        argv = argv[1:]
    if not argv or not label.strip():
        raise ValueError('RUN_INVALID: nonempty label and command required')
    cwd = Path(cwd or Path.cwd()).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('RUN_INVALID: cwd must be a directory')
    ordering = ('shared-pty' if same_terminal(sys.stdout.fileno(), sys.stderr.fileno())
                else 'observed-read-order')
    root = Path(state).expanduser().absolute() / 'receipts'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    rid = uuid.uuid4().hex
    log = root / (rid + '.log')
    fd = None
    try:
        os.fchmod(directory, 0o700)
        fd = os.open(log.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        digest, size = hashlib.sha256(), 0
        stderr_line_open = False
        def emit(block, target):
            nonlocal size, stderr_line_open
            write_all(fd, block)
            digest.update(block)
            size += len(block)
            destination = target
            write_all(destination, block)
            if destination == sys.stderr.fileno() or same_terminal(destination, sys.stderr.fileno()):
                stderr_line_open = not block.endswith(b'\n')
        before = git_snapshot(cwd)
        start = now()
        code = routed_capture(argv, str(cwd), emit)
        end = now()
        os.fsync(fd)
        # Publish only after capture completes. A failed capture leaves an orphan log, not a usable receipt.
        row = dict(schema=SCHEMA, id=rid, label=label, argv=argv, cwd=str(cwd),
                   started=start, finished=end, exit_code=code, log=log.name,
                   log_sha256=digest.hexdigest(), log_bytes=size, capture='routed',
                   log_order=ordering, **before)
        # Same owner-private locked/fchmod/fsync-file-and-directory writer as retros.
        retros.append(root / (rid + '.json'), row)
        os.fsync(directory)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)
    print(('\n' if stderr_line_open else '') + 'RUN_RECEIPT: ' + rid, file=sys.stderr, flush=True)
    return code


def read_locked(path, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('not a regular file')
        fcntl.flock(fd, fcntl.LOCK_SH)
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('bridge receipt/log must be owner-private (0600)')
        with os.fdopen(fd, 'rb', closefd=False) as source:
            return source.read()
    finally:
        os.close(fd)


def receipt(state, reference):
    if not isinstance(reference, str) or not reference:
        raise ValueError('receipt must be a nonempty id or JSON path')
    local_id = bool(ID.fullmatch(reference))
    path = (Path(state) / 'receipts' / (reference + '.json') if local_id else Path(reference).expanduser())
    data = json.loads(read_locked(path, private=local_id))
    if not isinstance(data, dict):
        raise ValueError('receipt must hold an object')
    native = data.get('schema') == SCHEMA
    if native:
        # Native paths are confined to the receipt's sibling log, even when referenced by JSON path.
        rid = data.get('id')
        if not isinstance(rid, str) or not ID.fullmatch(rid) or data.get('log') != rid + '.log':
            raise ValueError('invalid bridge log reference')
        read_locked(path, private=True)
        log = read_locked(path.with_name(data['log']), private=True)
        if type(data.get('log_bytes')) is not int or data['log_bytes'] != len(log):
            raise ValueError('log byte size mismatch')
        if data.get('log_sha256') != hashlib.sha256(log).hexdigest():
            raise ValueError('log sha256 mismatch')
        code, head, kind = data.get('exit_code'), data.get('head'), 'bridge'
    elif 'exitCode' in data and path.name == 'result.json':
        # Actual remote pool shape has no log path field. root/out refer to remote paths.
        code, head, kind = data['exitCode'], data.get('sourceCommit'), 'pool'
        log = read_locked(path.with_name('command.log'))
    elif not local_id and 'exit_code' in data and isinstance(data.get('log'), str) and data['log']:
        code, head, kind = data['exit_code'], data.get('head'), 'external'
        log_path = Path(data['log']).expanduser()
        log = read_locked(log_path if log_path.is_absolute() else path.parent / log_path)
    else:
        raise ValueError('unsupported receipt shape')
    if type(code) is not int:
        raise ValueError('receipt exit code must be an integer')
    if head in ('', None):
        head = None
    elif not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError('receipt head must be a full SHA or unknown')
    return dict(exit=code, head=head, kind=kind, log=log)


def check(state, rows_path, head):
    if not SHA.fullmatch(head):
        raise ValueError('HANDOVER_UNVERIFIED: head must be a full SHA')
    try:
        rows = json.loads(Path(rows_path).expanduser().read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError(f'HANDOVER_UNVERIFIED: rows unreadable: {error}') from error
    if not isinstance(rows, list) or not rows:
        raise ValueError('HANDOVER_UNVERIFIED: rows must be a nonempty JSON list')
    checked = []
    for index, row in enumerate(rows, 1):
        try:
            if not isinstance(row, dict) or not isinstance(row.get('claim'), str) or not row['claim'].strip():
                raise ValueError('claim must be nonempty text')
            if type(row.get('exit')) is not int:
                raise ValueError('row exit must be an integer')
            figures = row.get('figures')
            if not isinstance(figures, list) or not all(isinstance(f, str) and f for f in figures):
                raise ValueError('figures must be a list of nonempty strings')
            stated = row.get('head', head)
            if not isinstance(stated, str) or not SHA.fullmatch(stated):
                raise ValueError('row head must be a full SHA')
            found = receipt(state, row.get('receipt'))
            if found['exit'] != row['exit']:
                raise ValueError(f"exit mismatch: row {row['exit']}, receipt {found['exit']}")
            if found['head'] is not None and found['head'].lower() != stated.lower():
                raise ValueError(f"head mismatch: receipt {found['head']}, stated {stated}")
            for figure in figures:
                if figure.encode('utf-8') not in found['log']:
                    raise ValueError('figure not in log: ' + json.dumps(figure, ensure_ascii=False))
            checked.append({**row, 'head': stated, 'recorded_head': found['head'],
                            'receipt_kind': found['kind'], 'verified': True})
        except (ValueError, OSError, TypeError) as error:
            raise ValueError(f'HANDOVER_UNVERIFIED: row {index}: {error}') from error
    return checked


def cell(value):
    # JSON quoting preserves every codepoint; escaping pipes prevents injected table rows.
    return json.dumps(value, ensure_ascii=False).replace('|', '\\u007c')


def message(card, head, checked, note=None):
    lines = ['Verified handover', 'Card: ' + cell(card), 'Head: ' + cell(head),
             'Verified = exit, literal figures and known recorded HEAD checked locally; no provenance authentication.',
             '| Claim | Exit | Figures | Receipt reference | Verification | HEAD |',
             '| --- | --- | --- | --- | --- | --- |']
    for row in checked:
        mark = ('earlier/different head: ' if row['head'].lower() != head.lower() else 'handover head: ') + row['head']
        mark += '; recorded HEAD: ' + (row['recorded_head'] or 'unknown')
        lines.append('| ' + ' | '.join([cell(row['claim']), str(row['exit']), cell(row['figures']),
                                       cell(row['receipt']), 'verified (' + row['receipt_kind'] + ')', cell(mark)]) + ' |')
    if note is not None:
        lines.extend(['', '--- Unverified free-text note (verbatim) ---', note])
    body = '\n'.join(lines)
    if len(body.encode('utf-8')) > 32768:
        raise ValueError('HANDOVER_UNVERIFIED: message exceeds 32768 UTF-8 bytes')
    return body
