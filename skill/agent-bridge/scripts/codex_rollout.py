"""Inspect exactly bound Codex rollout files without exposing their contents."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import time


def file_stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def bound_rollout(value, sid, *, archived=False):
    path = Path(value)
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()
    directory = home / ('archived_sessions' if archived else 'sessions')
    if (not path.is_absolute() or path.resolve() != path
            or not path.is_relative_to(directory) or (archived and path.parent != directory)):
        raise ValueError('Rollout location differs')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise ValueError('Rollout is not owned and protected')
        stamp = file_stamp(info)
        meta = record(source.readline())
        if meta['type'] != 'session_meta' or meta['payload'].get('id') != sid:
            raise ValueError('Rollout identity differs')
        yield source, stamp
        if file_stamp(os.fstat(source.fileno())) != stamp or file_stamp(path.lstat()) != stamp:
            raise ValueError('Rollout changed during read')


def record(line):
    if not line.endswith(b'\n'):
        raise ValueError('Incomplete rollout record')
    row = json.loads(line)
    if not isinstance(row, dict) or not isinstance(row.get('type'), str):
        raise ValueError('Invalid rollout record')
    return row


def turn_state(value, sid, deadline):
    try:
        with bound_rollout(value, sid) as (source, stamp):
            latest, active_turn = 'unknown', None
            for line in source:
                if deadline is not None and time.monotonic() >= deadline:
                    return 'unknown', None
                row = record(line)
                if row['type'] != 'event_msg':
                    continue
                event = row['payload']
                kind = event.get('type')
                if kind not in ('task_started', 'task_complete', 'turn_aborted'):
                    continue
                turn = event.get('turn_id')
                if not isinstance(turn, str) or not turn:
                    return 'unknown', None
                if kind == 'task_started':
                    active_turn = turn
                    latest = 'open'
                elif active_turn is None or active_turn == turn:
                    active_turn = None
                    latest = 'terminal'
        return latest, stamp
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return 'unknown', None


def verify_archived(value, sid):
    try:
        with bound_rollout(value, sid, archived=True):
            pass
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return False
    return True
