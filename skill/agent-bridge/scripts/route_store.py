"""Owner-private, atomic routing snapshots. Never stores message/delivery data."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

import sessions
import codex_live

ROUTE_FIELDS = ('project', 'name', 'transport', 'cwd', 'session', 'executable')
CHANNEL_FIELDS = ('project', 'name', 'owner', 'description', 'generation', 'active', 'route')
MAX_SNAPSHOT = 4 * 1024 * 1024


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', value):
        raise ValueError('Invalid routing identifier')
    return value


def route_record(item, pinned=False):
    if not isinstance(item, dict):
        raise ValueError('Invalid route')
    result = {field: item[field] for field in ROUTE_FIELDS}
    for field in ('project', 'name'):
        identifier(result[field])
    if result['transport'] not in ('realtime', 'claude', 'claude-peer', 'codex', 'agy'):
        raise ValueError('Invalid transport')
    if not isinstance(result['cwd'], str) or not Path(result['cwd']).is_absolute():
        raise ValueError('Invalid workspace')
    if result['transport'] != 'realtime' or result['session'] is not None:
        if str(uuid.UUID(result['session'])) != result['session']:
            raise ValueError('Invalid session')
    if result['executable'] is not None and not isinstance(result['executable'], str):
        raise ValueError('Invalid executable')
    if pinned:
        result['channel'] = identifier(item['channel'])
        if type(item['generation']) is not int or item['generation'] < 1:
            raise ValueError('Invalid generation')
        result['generation'] = item['generation']
    return result


def channel_record(item):
    if not isinstance(item, dict):
        raise ValueError('Invalid channel')
    result = {field: item[field] for field in CHANNEL_FIELDS}
    for field in ('project', 'name', 'owner'):
        identifier(result[field])
    if (not isinstance(result['description'], str) or not result['description'].strip()
            or type(result['generation']) is not int or result['generation'] < 1
            or type(result['active']) is not bool):
        raise ValueError('Invalid channel fields')
    route = result['route'] = route_record(result['route'], pinned=True)
    if (route['project'] != result['project'] or route['name'] != result['owner']
            or route['channel'] != result['name']
            or route['generation'] > result['generation']
            or (result['active'] and route['generation'] != result['generation'])):
        raise ValueError('Inconsistent channel route')
    return result


def verify_route(route):
    """Re-check the exact UUID and workspace; no sends, turns, or Claude CLI."""
    cwd = Path(route['cwd']).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('Workspace unavailable')
    transport = route['transport']
    if transport in ('realtime', 'claude'):
        # Dedicated CLI routes have no live peer contract. Never launch a CLI
        # to restore them; their UUID/workspace are validated, not liveness.
        return
    if transport == 'claude-peer':
        target = sessions.resolve_claude_peer(route['session'])
        info = Path(target['socket']).stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Peer socket unavailable')
    else:
        observation = sessions.inspect_session(
            transport, route['session'], route['executable'], codex_mode='auto')
        target = observation['session']
    if (target['id'] != route['session']
            or Path(target['cwd']).resolve(strict=True) != cwd):
        raise ValueError('Session or workspace differs')
    if transport == 'codex':
        # Auto discovery can read saved metadata when the shared proxy is
        # absent. Its runtime status is not Desktop liveness: the independent
        # IPC owner must confirm this exact UUID and untrusted-input support.
        with codex_live.Client(timeout=3) as client:
            client.owner(route['session'])


class RouteStore:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().absolute()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError('Routing state directory must be owner-private (0700)')
        self.path = self.directory / 'routes.json'
        fd = os.open(self.directory / 'routes.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.close()
            raise ValueError('Routing state directory is already in use') from error

    def close(self):
        self.lock.close()

    def save(self, endpoints, channels):
        snapshot = dict(version=1,
                        endpoints=[route_record(item) for item in endpoints.values()],
                        channels=[channel_record(item) for item in channels.values()])
        data = (json.dumps(snapshot, ensure_ascii=False) + '\n').encode()
        if len(data) > MAX_SNAPSHOT:
            raise ValueError('Routing snapshot exceeds size limit')
        name = None
        try:
            fd, name = tempfile.mkstemp(prefix='.routes-', dir=self.directory)
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            # Last fallible commit operation. Before this, the old file remains
            # intact and the caller must not swap its in-memory routing maps.
            os.replace(name, self.path)
            name = None
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}, {}
        with os.fdopen(fd, 'r', encoding='utf-8') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_SNAPSHOT):
                raise ValueError('Routing snapshot must be an owner-private regular file within size limit')
            try:
                data = json.load(source)
            except (ValueError, UnicodeError):
                raise ValueError('Invalid routing snapshot; refusing to overwrite it') from None
        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('endpoints'), list) or not isinstance(data.get('channels'), list)):
            raise ValueError('Unsupported routing snapshot; refusing to overwrite it')
        endpoints, channels = {}, {}
        dropped = False
        verified = set()
        for kind, items, output, normalize in (
                ('endpoint', data['endpoints'], endpoints, route_record),
                ('channel', data['channels'], channels, channel_record)):
            for index, raw in enumerate(items):
                item = None
                try:
                    item = normalize(raw)
                    route = item['route'] if kind == 'channel' else item
                    identity = tuple(route.get(field) for field in ROUTE_FIELDS)
                    if identity not in verified:
                        verify_route(route)
                        verified.add(identity)
                    key = (item['project'], item['name'])
                    if key in output:
                        raise ValueError('Duplicate routing key')
                    output[key] = item
                except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
                    # Exceptions from discovery can contain peer tokens/socket
                    # paths or provider payloads. Log only validated aliases and index.
                    diagnostic = dict(routing_drop=kind, index=index,
                                      reason='invalid or unavailable route')
                    if item is not None:
                        diagnostic.update(project=item['project'], name=item['name'])
                    print(json.dumps(diagnostic),
                          file=sys.stderr, flush=True)
                    dropped = True
        if dropped:
            self.save(endpoints, channels)
        return endpoints, channels
