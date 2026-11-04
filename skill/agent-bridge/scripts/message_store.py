"""Owner-private correspondence checkpoint. Runtime credentials never enter it."""
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
import uuid

import route_store

MESSAGE_FIELDS = ('id', 'conversation', 'project', 'sender', 'recipient', 'channel',
                  'sender_channel', 'body', 'reply_to', 'state', 'created')
INTENT_FIELDS = ('project', 'sender', 'recipient', 'channel', 'sender_channel', 'body', 'key', 'reply_to')
STATES = frozenset(('pending', 'dispatching', 'submitted', 'received', 'in_progress',
                    'uncertain', 'done', 'answered', 'failed'))
FRAME_BYTES = 1024 * 1024
SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_RECEIPTS = 8192


# Shared with routing snapshots, including full-flush and directory barriers.
atomic_write = route_store.atomic_write

MALFORMED_CONTENT = (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError, OverflowError)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False) + '\n').encode()


def message_record(raw):
    result = {key: raw[key] for key in MESSAGE_FIELDS}
    for key in ('id', 'conversation'):
        if str(uuid.UUID(result[key])) != result[key]:
            raise ValueError('Invalid message identity')
    if result['reply_to'] is not None:
        uuid.UUID(result['reply_to'])
    if result['state'] not in STATES:
        raise ValueError('Invalid message state')
    if not isinstance(result['body'], str) or not 1 <= len(result['body'].encode()) <= 32768:
        raise ValueError('Invalid message body')
    for field in ('project', 'sender', 'recipient'):
        route_store.identifier(result[field])
    if not isinstance(result['created'], str):
        raise ValueError('Invalid message time')
    for field in ('route', 'return_route'):
        result[field] = route_store.route_record(raw[field], pinned=bool(raw[field].get('channel')))
    if (result['route']['project'] != result['project']
            or result['route']['name'] != result['recipient']
            or result['return_route']['name'] != result['sender']):
        raise ValueError('Inconsistent message routes')
    if raw.get('acknowledged_at') is not None:
        if not isinstance(raw['acknowledged_at'], str):
            raise ValueError('Invalid acknowledgement time')
        result['acknowledged_at'] = raw['acknowledged_at']
    # Provider diagnostics may contain secrets. Neither diagnostics nor event
    # payloads are needed to reconcile delivery, so never serialize them.
    result.update(note=None, events=[])
    if len(encoded(result)) > FRAME_BYTES:
        raise ValueError('Correspondence message exceeds frame byte limit')
    return result


def receipt_record(key, value):
    expiry = value['expires']
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
        raise ValueError('Invalid receipt expiry')
    if (len(key) != 3 or any(not isinstance(k, str) for k in key)
            or len(key[0]) != 64 or len(key[1]) != 64):
        raise ValueError('Invalid receipt binding')
    fields = ('id', 'acknowledged', 'acknowledged_at', 'broker_instance', 'reconcile_until')
    result = {field: value['result'][field] for field in fields}
    if (result['id'] != key[2] or result['acknowledged'] is not True
            or not isinstance(result['acknowledged_at'], str)
            or result['reconcile_until'] != value['expires']):
        raise ValueError('Invalid receipt')
    uuid.UUID(result['id'])
    uuid.UUID(result['broker_instance'])
    normalized = dict(result=result, expires=value['expires'])
    if len(encoded(normalized)) > 1024:
        raise ValueError('Receipt exceeds byte limit')
    return [list(key), normalized]


def snapshot_record(value):
    expiry = value['expires']
    if (type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0
            or not isinstance(value['owner'], str) or len(value['owner']) != 64
            or not isinstance(value['context'], str) or len(value['context']) != 64):
        raise ValueError('Invalid snapshot binding or expiry')
    fields = ('id', 'agent_session_id', 'created_at', 'project', 'sender', 'recipient',
              'provenance', 'reply_to', 'state', 'acknowledged', 'acknowledged_at',
              'body_chars', 'body_bytes', 'body_sha256')
    items = []
    for item in value['items']:
        metadata = {field: item[field] for field in fields}
        for address in ('sender', 'recipient'):
            metadata[address] = {field: item[address].get(field)
                                 for field in ('project', 'name', 'transport', 'channel', 'generation')}
        items.append(metadata)
    size = len(json.dumps(items, ensure_ascii=False).encode())
    if size != value['bytes'] or size > 4 * 1024 * 1024:
        raise ValueError('Invalid inbox snapshot byte count')
    return dict(owner=value['owner'], context=value['context'], items=items,
                expires=value['expires'], bytes=size)


def loss_identity(raw):
    item = {field: raw[field] for field in ('id', 'sender', 'recipient', 'created')}
    if any(value is not None and not isinstance(value, str) for value in item.values()):
        raise ValueError('Invalid loss identity')
    return item


def loss_report(raw):
    if (not isinstance(raw, dict) or not isinstance(raw['file'], str)
            or not isinstance(raw['reason'], str) or type(raw['detected_at']) is not int
            or type(raw['unknown_identities']) is not bool or not isinstance(raw['lost'], list)):
        raise ValueError('Invalid loss report')
    report = {field: raw[field] for field in ('file', 'reason', 'detected_at', 'unknown_identities')}
    report['lost'] = [loss_identity(item) for item in raw['lost']]
    return report


class MessageStore:
    def __init__(self, directory, max_messages):
        # RouteStore already owns the directory's exclusive lock and validates
        # its owner and 0700 permissions for the whole lifetime of this store.
        self.path = Path(directory) / 'correspondence.json'
        self.manifest = self.path.with_name('correspondence-index.json')
        self.loss = self.path.with_name('correspondence-lost.json').exists()
        self.max_messages = max_messages
        self.max_bytes = max_messages * FRAME_BYTES + SNAPSHOT_BYTES + MAX_RECEIPTS * 1024

    def read(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > self.max_bytes):
                raise ValueError('Unsafe or oversized correspondence file')
            try:
                return json.load(source)
            except RecursionError:
                raise ValueError('Correspondence JSON nesting limit exceeded') from None

    def save(self, state):
        messages = [message_record(m) for m in state.messages.values()]
        if len(messages) > self.max_messages:
            raise ValueError('Correspondence message count limit reached')
        current = time.time()
        receipts = [receipt_record(key, value) for key, value in state.inbox_receipts.items()
                    if value['expires'] > current]
        snapshots = {key: snapshot_record(value) for key, value in state.inbox_snapshots.items()
                     if value['expires'] > current}
        if len(receipts) > MAX_RECEIPTS or len(snapshots) > 16:
            raise ValueError('Correspondence inbox count limit reached')
        if sum(value['bytes'] for value in snapshots.values()) > SNAPSHOT_BYTES:
            raise ValueError('Correspondence inbox byte limit reached')
        keys = [[key, {field: intent.get(field) for field in INTENT_FIELDS}, mid]
                for key, (intent, mid) in state.keys.items() if mid in state.messages]
        data = encoded(dict(version=1, messages=messages, keys=keys, receipts=receipts, snapshots=snapshots))
        if len(data) > self.max_bytes:
            raise ValueError('Correspondence checkpoint byte limit reached')
        # The bounded metadata index is committed first. It is never replayed as
        # mail. If the payload file is later truncated, IDs remain diagnosable.
        index = [{key: m[key] for key in ('id', 'sender', 'recipient', 'created')} for m in messages]
        atomic_write(self.manifest, encoded(dict(version=1, messages=index)))
        atomic_write(self.path, data)

    def load(self):
        self.read_loss_reports()
        empty = dict(messages=[], keys=[], receipts=[], snapshots={})
        try:
            raw = self.read(self.path)
        except FileNotFoundError:
            if not self.manifest.exists():
                return empty
            return self.lost('checkpoint missing', empty)
        except (ValueError, OSError, UnicodeError):
            return self.lost('checkpoint unreadable, unsafe or corrupt', empty)
        try:
            if (not isinstance(raw, dict) or raw['version'] != 1
                    or not isinstance(raw['messages'], list) or len(raw['messages']) > self.max_messages):
                raise ValueError('Invalid checkpoint version or count')
            messages = [message_record(m) for m in raw['messages']]
            ids = {m['id'] for m in messages}
            if len(ids) != len(messages):
                raise ValueError('Duplicate message')
            keys = raw['keys']
            if (not isinstance(keys, list) or len(keys) > len(messages)
                    or any(not isinstance(row, list) or len(row) != 3 for row in keys)):
                raise ValueError('Invalid idempotency rows')
            if len({row[0] for row in keys}) != len(keys):
                raise ValueError('Invalid idempotency count')
            for key, intent, mid in keys:
                if (not isinstance(key, str) or not 1 <= len(key) <= 256 or mid not in ids
                        or set(intent) != set(INTENT_FIELDS) or intent['key'] != key
                        or any(value is not None and not isinstance(value, str) for value in intent.values())):
                    raise ValueError('Invalid idempotency record')
            receipts, snapshots = raw['receipts'], raw['snapshots']
            if len(receipts) > MAX_RECEIPTS or len(snapshots) > 16:
                raise ValueError('Invalid inbox count')
            receipts = [receipt_record(key, value) for key, value in receipts]
            snapshots = {key: snapshot_record(value) for key, value in snapshots.items()}
            total = sum(value['bytes'] for value in snapshots.values())
            if total > SNAPSHOT_BYTES:
                raise ValueError('Invalid inbox byte count')
            return dict(messages=messages, keys=keys, receipts=receipts, snapshots=snapshots)
        except MALFORMED_CONTENT:
            return self.lost('invalid checkpoint records; entire checkpoint dropped', empty)

    def read_loss_reports(self):
        path = self.path.with_name('correspondence-lost.json')
        try:
            raw = self.read(path)
        except FileNotFoundError:
            return []
        except MALFORMED_CONTENT:
            return self.quarantine_loss_ledger(path)
        try:
            previous = raw.get('previous', [])
            if not isinstance(previous, list):
                raise ValueError('Invalid loss history')
            return [loss_report(item) for item in previous] + [loss_report(raw)]
        except MALFORMED_CONTENT:
            return self.quarantine_loss_ledger(path)

    def quarantine_loss_ledger(self, path):
        quarantine = path.with_name('correspondence-lost-corrupt.json')
        os.replace(path, quarantine)
        route_store.sync_directory(path.parent)
        self.loss = True
        report = dict(file=path.name, reason='Malformed loss ledger; previous history quarantined',
                      detected_at=time.time_ns(), lost=[], unknown_identities=True)
        atomic_write(path, encoded(dict(report, previous=[])))
        print(json.dumps(dict(correspondence_drop=path.name, reason=report['reason'],
                              quarantine=quarantine.name, lost_ids=[], unknown_identities=True)),
              file=sys.stderr, flush=True)
        return [report]

    def lost(self, reason, empty):
        try:
            index = self.read(self.manifest)['messages']
            if not isinstance(index, list):
                raise ValueError('Invalid correspondence index')
            records = [loss_identity(row) for row in index]
        except (*MALFORMED_CONTENT, OSError):
            records = [dict(id=None, sender=None, recipient=None, created=None)]
        self.loss = True
        stamp = time.time_ns()
        report = dict(file=self.path.name, reason=reason, detected_at=stamp, lost=records,
                      unknown_identities=any(row['id'] is None for row in records))
        # Keep bounded, durable loss receipts. Quarantine the damaged file
        # once, without retaining unlimited copies or ever replaying its data.
        loss_path = self.path.with_name('correspondence-lost.json')
        reports = self.read_loss_reports()
        # Keep earlier incidents as well. Refuse further recovery visibly at the
        # same byte ceiling instead of silently overwriting a prior loss receipt.
        report['previous'] = reports
        loss_data = encoded(report)
        if len(loss_data) > self.max_bytes:
            raise ValueError('Correspondence loss ledger byte limit reached; owner intervention required')
        atomic_write(loss_path, loss_data)
        if self.path.exists() and not self.path.is_symlink():
            os.replace(self.path, self.path.with_name('correspondence-corrupt.json'))
        print(json.dumps(dict(correspondence_drop=self.path.name, reason=reason,
                              lost_ids=[row['id'] for row in records])), file=sys.stderr, flush=True)
        return empty
