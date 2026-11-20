"""Single-process broker: durable routing and bounded correspondence."""
import asyncio
from collections import deque, namedtuple
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import route_store
import codex_preview
import message_store
import retention
import usage
import provider_failures
import codex_failures
import agy_live
import peer_wake

DEFAULT_SOCKET = Path.home() / '.local/share/agent-bridge/bridge.sock'
MAX_FRAME = 1024 * 1024
INBOX_TTL = 3600
INBOX_MAX_SNAPSHOTS = 16
INBOX_SNAPSHOT_BYTES = 4 * 1024 * 1024
INBOX_TOTAL_BYTES = 16 * 1024 * 1024
INBOX_PAGE_BYTES = 128 * 1024
INBOX_MAX_RECEIPTS = 8192
CLOSE_KEY_PREFIX = 'agent-bridge-close:'
INBOX_REPLY_KEY_PREFIX = 'agent-bridge-inbox-reply:'
PROVIDER_NOTICE_KEY_PREFIXES = tuple(event + ':' for event in (
    provider_failures.DETECTED_EVENT, provider_failures.RETRY_EVENT, provider_failures.EXHAUSTED,
    provider_failures.UNSAFE, provider_failures.UNCERTAIN))
CLOSE_OPERATOR_FILE = 'close-operator.json'
CLOSING_ROUTE_ERROR = 'Codex route is closing'
READ_ONLY_OPS = ('ping', 'routes', 'codex-routes', 'channels', 'show', 'conversation', 'inbox', 'retention-plan')
CHECKPOINT_FIELDS = ('messages', 'compact_messages', 'keys', 'inbox_receipts', 'inbox_snapshots')
Checkpoint = namedtuple('Checkpoint', CHECKPOINT_FIELDS)


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_-]*',value):
        raise ValueError('Use a lowercase identifier')
    return value


def wire(value):
    data = (json.dumps(value,ensure_ascii=False)+'\n').encode()
    if len(data) > MAX_FRAME:
        raise ValueError('Response exceeds frame limit; use a smaller query')
    return data


def route_key(route):
    return tuple(route.get(k) for k in ('project','name','transport','session','cwd','channel','generation'))


def same_archive_endpoint(left, right):
    fields = ('transport', 'session', 'cwd')
    return all(left.get(field) == right.get(field) for field in fields)


def same_codex_thread(left, right):
    return left['transport'] == right['transport'] == 'codex' and left['session'] == right['session']


TERMINAL_STATES = frozenset(('done', 'answered', 'failed'))
UNKNOWN_MESSAGE_ERROR = 'Unknown message; broker may have restarted'
REGISTER_CONFLICT_ERROR = 'Route name is already in use; use a different name'
LEGACY_CAPACITY_ERROR = ('In-memory message limit reached: no evictable terminal messages; '
                         'pending or protected work is retained')
TTL_CAPACITY_ERROR = ('Active-message limit reached: TTL or protected work prevents safe cleanup; '
                      'inspect retention --live; no messages evicted before TTL')
MESSAGE_CAPACITY_ERRORS = frozenset((LEGACY_CAPACITY_ERROR, TTL_CAPACITY_ERROR))
GUARDED_CLAIM_WINDOW = 10
CLAIM_RELEASE_NOTE = 'Claude claim response was lost. The claim was released before any peer write.'


def delivery_destination(route):
    return route['transport'], route['session']


def valid_claim_token(token):
    if not isinstance(token, str) or len(token) != 36:
        return False
    try:
        value = uuid.UUID(token)
    except ValueError:
        return False
    return value.version == 4 and str(value) == token


def guarded_claim_fields(args):
    token = args.get('claim_token')
    if not valid_claim_token(token):
        raise ValueError('Invalid delivery claim token')
    admit_until = args.get('admit_until')
    if type(admit_until) not in (int, float) or not 0 < admit_until <= time.monotonic() + GUARDED_CLAIM_WINDOW:
        raise ValueError('Invalid delivery claim admission deadline')
    return token, admit_until


def public_message(message):
    if ('dispatch_claim' not in message and 'dispatch_claim_deadline' not in message
            and 'reply_deadline_at' not in message):
        return message
    result = {key:value for key,value in message.items()
              if key not in ('dispatch_claim', 'dispatch_claim_deadline')}
    result.update(retention.request_status(message, time.time()))
    return result


def build_id(root=None):
    """Name the source a process runs: a digest of every module beside this one."""
    digest = hashlib.sha256()
    for path in sorted(Path(root or Path(__file__).resolve().parent).glob('*.py')):
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()[:12]


class BrokerConnectError(ValueError):
    """A socket connection failed before a broker request was sent."""

    def __init__(self, error):
        super().__init__('Broker unavailable; start bridge.py serve for this socket')
        self.errno = error.errno


class BrokerResponseError(ValueError):
    """The broker did not return a usable response to a request."""


class IdempotencyConflict(ValueError):
    """The same key was used for a different message intent."""


class State:
    def __init__(self, max_messages=2048, store=None, retention_ttl=False, retention_prepare=False,
                 *, checkpoint_executor=None):
        if max_messages < 1:
            raise ValueError('Message retention size must be positive')
        self.instance = str(uuid.uuid4())
        self.build = build_id()  # Read once: the source this process started from.
        self.max_messages = max_messages
        if retention_ttl and max_messages > 2048:
            raise ValueError('TTL active-message limit cannot exceed 2048')
        if retention_ttl and retention_prepare:
            raise ValueError('Retention prepare and TTL modes are mutually exclusive')
        self.retention_ttl = retention_ttl
        self.retention_prepare = retention_prepare
        self.compact_messages = {}
        self.endpoints, self.channels, self.messages, self.keys = {}, {}, {}, {}
        self.events = deque(maxlen=1024)
        self.sequence = 0
        self.subscribers = []
        self.answers = {}
        self.waiters = {}
        self.inbox_snapshots = {}
        self.inbox_receipts = {}
        self.mutation_lock = asyncio.Lock()
        self.active_mutation = None
        self.mutations_closed = False
        self.checkpoint_executor = checkpoint_executor
        self.store = store
        if store is not None:
            self.endpoints, self.channels = store.load()
        self.correspondence = None
        self.pending_publications = None
        if store is not None:
            self.correspondence = message_store.MessageStore(store.directory, max_messages)
            saved = self.correspondence.load()
            self.messages = {m['id']: m for m in saved['messages']}
            self.compact_messages = {m['id']: m for m in saved['compact']}
            self.keys = {key: (intent, mid) for key, intent, mid in saved['keys']}
            self.inbox_receipts = {tuple(key): value for key, value in saved['receipts']}
            self.inbox_snapshots = saved['snapshots']
            before = deepcopy(self.messages)
            self.make_room(restoring=True)
            dropped = [message_store.loss_identity(m) for mid, m in before.items()
                       if mid not in self.messages and mid not in self.compact_messages]
            if dropped and not self.retention_ttl:
                self.correspondence.record_loss('message retention limit reduced; oldest unguarded terminal messages evicted', dropped)
            if len(self.messages) > self.max_messages:
                print(json.dumps(dict(correspondence_over_limit=self.correspondence.path.name,
                                      kept=len(self.messages), limit=self.max_messages)),
                      file=sys.stderr, flush=True)
            reply_parents = {m['reply_to'] for m in self.messages.values()}
            for mid, message in (self.messages | self.compact_messages).items():
                self.answers[mid] = asyncio.Event()
                if mid in reply_parents or ('close_request' in message and message['state'] == 'done'):
                    self.answers[mid].set()
                if message['state'] == 'dispatching':
                    message.update(state='uncertain', note='Broker restarted during delivery; resolve explicitly')
            # Every broker epoch reconciles the checkpoint and metadata index,
            # including removal of receipts/snapshots bound to the retired epoch.
            # Periodic no-op sweeps still avoid cloning and checkpoint writes.
            self.correspondence.save(self)
        # Volatile endpoint history distinguishes unused inbox pins from removed
        # return routes. Restored endpoints are already initialized this epoch.
        self.inbox_initialized_routes = set(self.endpoints)

    def routing_change(self, collection, key, item):
        endpoints, channels = self.endpoints.copy(), self.channels.copy()
        (endpoints if collection == 'endpoints' else channels)[key] = item
        # Commit to disk before accepting the action in memory. Save failures
        # leave both maps and the previous complete file untouched.
        if self.store is not None:
            self.store.save(endpoints, channels)
        self.endpoints, self.channels = endpoints, channels
        if collection == 'endpoints':
            self.inbox_initialized_routes.add(key)
        return item

    def routing_remove(self, collection, key):
        endpoints, channels = self.endpoints.copy(), self.channels.copy()
        item = (endpoints if collection == 'endpoints' else channels).pop(key)
        if self.store is not None:
            self.store.save(endpoints, channels)
        self.endpoints, self.channels = endpoints, channels
        return item

    def make_room(self, parent=None, *, restoring=False):
        if self.retention_prepare:
            self.sweep_retention(parent=parent)
        if self.retention_ttl:
            self.sweep_retention(parent=parent)
            if not restoring and len(self.messages) >= self.max_messages:
                raise ValueError(TTL_CAPACITY_ERROR)
            return
        target = self.max_messages if restoring else self.max_messages - 1
        if len(self.messages) <= target:
            return
        # Plan evictions first. A refused send must also be atomic for an
        # in-memory broker whose retention limit has been lowered.
        remaining = self.messages.copy()
        evicted = []
        while len(remaining) > target:
            # On restore every retained reply already exists. Protect its parent
            # just as an incoming reply protects its parent during normal sends.
            parents = {m['reply_to'] for m in remaining.values()} if restoring else set()
            # Insertion order defines oldest. Keep a reply's parent, active waits,
            # and replies whose unfinished parent may still need their answer.
            for mid, message in remaining.items():
                ancestor = remaining.get(message['reply_to'])
                if (mid == parent or mid in parents or self.waiters.get(mid)
                        or message['state'] not in TERMINAL_STATES
                        or (message.get('provider_retry') is not None
                            and message['provider_retry']['status'] != 'recovered')
                        or (ancestor and (ancestor['state'] not in TERMINAL_STATES
                                          or self.waiters.get(ancestor['id'])))):
                    continue
                del remaining[mid]
                evicted.append(mid)
                break
            else:
                if restoring:
                    break  # Lowering a setting cannot discard protected mail.
                raise ValueError(LEGACY_CAPACITY_ERROR)
        for mid in evicted:
            del self.messages[mid]
            self.answers.pop(mid, None)
        self.keys = {key: value for key, value in self.keys.items()
                     if value[1] in remaining or value[1] in self.compact_messages}

    def retention_plan(self, current=None, parent=None):
        return retention.plan(self.messages, self.compact_messages, self.waiters,
                              time.time() if current is None else current, self.max_messages, parent)

    def retention_changes(self, planned):
        return bool(planned['adopt'] or self.retention_ttl and (planned['drop'] or planned['compact'] or planned['remove']))

    def sweep_retention(self, current=None, parent=None, planned=None):
        if not self.retention_ttl and not self.retention_prepare:
            return False
        current = time.time() if current is None else current
        planned = self.retention_plan(current, parent) if planned is None else planned
        if not self.retention_changes(planned):
            return False
        for mid, since in planned['adopt'].items():
            self.messages[mid]['retention_adopted_at'] = since
        removed = []
        if self.retention_ttl:
            sources = retention.provider_sources(self.messages, self.compact_messages)
            for mid in planned['remove']:
                message = self.messages.get(mid)
                # A reused plan must never remove work that since advanced or
                # acquired a waiter/provider dependency before the write.
                if message is not None and retention.age_removable(
                        message, self.waiters, sources, current, parent):
                    del self.messages[mid]
                    self.answers.pop(mid, None)
                    removed.append(mid)
            for mid in planned['drop']:
                del self.compact_messages[mid]
                self.answers.pop(mid, None)
            for mid in planned['compact']:
                self.compact_messages[mid] = retention.compact_message(self.messages.pop(mid), current, planned['anchors'][mid])
            retained = self.messages.keys() | self.compact_messages.keys()
            compacted = set(planned['compact'])
            self.keys = {key: ({'fingerprint': retention.fingerprint(intent)}
                               if mid in compacted else intent, mid)
                         for key, (intent, mid) in self.keys.items() if mid in retained}
        return bool(planned['adopt'] or self.retention_ttl and (removed or planned['drop'] or planned['compact']))

    def get(self, mid):
        if mid in self.messages:
            return self.messages[mid]
        if mid in self.compact_messages:
            return self.compact_messages[mid]
        raise ValueError(UNKNOWN_MESSAGE_ERROR)

    def delivery_blockers(self):
        blockers = {}
        for message in self.messages.values():
            if message['state'] in ('dispatching', 'uncertain'):
                destination = delivery_destination(message['route'])
                blockers.setdefault(destination, {'id':message['id'], 'state':message['state'],
                                                   'since':message.get('dispatch_started_at')})
        return blockers

    @staticmethod
    def delivery_wait(blocker):
        note = f"pending behind {blocker['id']}"
        if blocker['since'] is not None:
            note += f" since {blocker['since']}"
        return {'blocked_by':blocker, 'note':note}

    def pending_delivery(self, message, blockers):
        if message['state'] == 'pending':
            blocker = blockers.get(delivery_destination(message['route']))
            if blocker:
                return self.delivery_wait(blocker)
        return {}

    def require_open_route(self, route):
        if route['transport'] == 'codex' and any(
                'closing' in item and same_codex_thread(route, item) for item in self.endpoints.values()):
            raise ValueError(CLOSING_ROUTE_ERROR)

    def route(self, project, name=None, channel=None):
        if channel:
            item = self.channels.get((project,channel))
            if not item or not item['active']:
                raise ValueError('Channel is missing or closed')
            item = item['route']
        else:
            item = self.endpoints.get((project,name))
            if not item:
                raise ValueError('Unregistered endpoint')
        self.require_open_route(item)
        return deepcopy(item)

    def matches(self, sub, event):
        m = event['message']
        destinations = [m['route']]
        if event['type'] == 'status':
            destinations.append(m['return_route'])
        return any(route_key(route) == sub['route'] for route in destinations)

    def publish(self, m, kind):
        m = public_message(m)
        if self.pending_publications is not None:
            self.pending_publications.append((deepcopy(m), kind))
            return
        self.sequence += 1
        event = {'seq':self.sequence,'instance':self.instance,'type':kind,'message':deepcopy(m)}
        self.events.append(event)
        for sub in self.subscribers:
            if sub['overflow']:
                continue
            if self.matches(sub,event):
                try:
                    sub['queue'].put_nowait(event)
                except asyncio.QueueFull:
                    # Disconnect visibly instead of silently dropping messages.
                    while not sub['queue'].empty():
                        sub['queue'].get_nowait()
                    sub['queue'].put_nowait({'error':'Subscriber fell behind; reconnect and inspect inbox'})
                    sub['overflow'] = True

    def status(self, m, state, note=None, event=None, reason=None):
        m.update(state=state,note=note)
        entry = {'state':state,'note':note,'at':now()}
        m.pop('reason', None)
        if reason is not None:
            m['reason'] = entry['reason'] = reason
        if state in ('done', 'answered'):
            m.setdefault('completed_at', time.time())
        if state == 'received' or (state == 'submitted' and m['route']['transport'] in retention.PUSH_TRANSPORTS):
            m.setdefault('delivered_at', time.time())
        if state == 'pending':
            m.pop('delivered_at', None)
        if m.get('body_expired'):
            # A late answer changes correlation metadata, never recreates content.
            m['events'] = []
            m['note'] = None
        if state == 'dispatching':
            m['dispatch_started_at'] = entry['at']
        else:
            m.pop('dispatch_claim', None)
            m.pop('dispatch_claim_deadline', None)
            if state != 'uncertain':
                m.pop('dispatch_started_at', None)
        if event:
            entry['event'] = event
        m['events'].append(entry)
        m['events'] = m['events'][-100:]
        self.publish(m,'status')
        if self.pending_publications is None and 'close_request' in m and state == 'done':
            self.answers[m['id']].set()
        return m

    def post(self, args, reply_to=None, *, close_request=None, inbox_reply=False):
        body, key = args['body'], args['key']
        if not isinstance(body,str) or not body.strip() or len(body.encode()) > 32768:
            raise ValueError('Message must contain 1–32768 UTF-8 bytes')
        if not isinstance(key,str) or not key.strip() or len(key)>256:
            raise ValueError('Provide an idempotency key of at most 256 characters')
        if key.startswith(CLOSE_KEY_PREFIX) and close_request is None:
            raise ValueError('Idempotency key prefix is reserved for close handoffs')
        if key.startswith(INBOX_REPLY_KEY_PREFIX) and not inbox_reply:
            raise ValueError('Idempotency key prefix is reserved for MCP inbox replies')
        intent = {k:args.get(k) for k in ('project','sender','recipient','channel','sender_channel','body','key')}
        intent['reply_to'] = reply_to
        one_way = args.get('one_way', False)
        deadline = args.get('reply_deadline_seconds')
        if type(one_way) is not bool:
            raise ValueError('one_way must be boolean')
        if deadline is not None and (not retention.valid_time(deadline) or deadline > retention.LOOKUP_TTL):
            raise ValueError('Reply deadline must be positive and at most 604800 seconds')
        if one_way and (deadline is not None or reply_to):
            raise ValueError('One-way intent cannot include a reply deadline or be a reply')
        if one_way:
            intent['one_way'] = True
        if deadline is not None:
            intent['reply_deadline_seconds'] = deadline
        if key in self.keys:
            original_intent, mid = self.keys[key]
            if not retention.matches(original_intent, intent):
                raise IdempotencyConflict('Idempotency key already used with different content')
            return self.get(mid)
        if reply_to:
            parent = self.get(reply_to)
            if 'close_request' in parent:
                raise ValueError('Codex close handoffs use native archive verification')
            destination, origin = deepcopy(parent['return_route']), deepcopy(parent['route'])
            self.require_open_route(destination)
            self.require_open_route(origin)
            self.require_open_route(destination)
            self.require_open_route(origin)
            conversation = parent['conversation']
        else:
            destination = self.route(args['project'],args.get('recipient'),args.get('channel'))
            origin = self.route(args['project'],args.get('sender'),args.get('sender_channel'))
            if route_key(destination)==route_key(origin):
                raise ValueError('Sender and recipient addresses must differ')
            conversation = str(uuid.uuid4())
        # Validate both addresses and the reply parent before removing history.
        self.make_room(reply_to)
        mid = str(uuid.uuid4())
        m = {'id':mid,'conversation':conversation,'project':destination['project'],
             'sender':origin['name'],'recipient':destination['name'],'route':destination,
             'return_route':origin,'channel':destination.get('channel'),
             'sender_channel':origin.get('channel'),'body':body,'reply_to':reply_to,
             'state':'pending','created':now(),'events':[],'note':None}
        if close_request is not None:
            m['close_request'] = close_request
        if one_way:
            m['one_way'] = True
        if deadline is not None:
            m['reply_deadline_at'] = time.time() + deadline
        self.messages[mid] = m
        self.keys[key] = (intent,mid)
        self.answers[mid] = asyncio.Event()
        self.publish(m,'message')
        if reply_to:
            parent['last_reply_at'] = time.time()
            self.status(parent,'answered',mid)
            if self.pending_publications is None:
                self.answers[reply_to].set()
        return m

    @staticmethod
    def inbox_metadata(message, slot):
        def address(route):
            return {key: route.get(key) for key in ('project', 'name', 'transport', 'channel', 'generation')}
        body = message['body']
        return {'id': message['id'], 'agent_session_id': slot,
                'created_at': message['created'], 'project': message['project'],
                'sender': address(message['return_route']), 'recipient': address(message['route']),
                'provenance': 'broker_route', 'reply_to': None, 'state': message['state'],
                'acknowledged': message.get('acknowledged_at') is not None,
                'acknowledged_at': message.get('acknowledged_at'),
                'body_chars': len(body), 'body_bytes': len(body.encode('utf-8')),
                'body_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest()}

    def inbox_action(self, args):
        # Runs in the same event-loop turn as route/epoch checks and inbox mutations.
        if args.get('instance') is not None and args['instance'] != self.instance:
            raise ValueError('BROKER_RESTARTED')
        if args['op'] == 'inbox_reply' and args.get('instance') is None:
            raise ValueError('INVALID_ARGUMENT')
        owner, context, routes = args['owner'], args['context'], args['routes']
        if (not isinstance(owner, str) or len(owner) != 64 or not isinstance(context, str)
                or len(context) != 64 or not isinstance(routes, list) or len(routes) > 64):
            raise ValueError('INVALID_ARGUMENT')
        selected = {}
        uninitialized = []
        for entry in routes:
            expected = entry['route']
            key = (expected['project'], expected['name'])
            actual = self.endpoints.get(key)
            if (args['op'] == 'inbox_list' and entry['slot'] in args.get('uninitialized_pins', ())
                    and actual is None and key not in self.inbox_initialized_routes):
                uninitialized.append(entry['slot'])
                continue  # A configured pin need not have sent its first message.
            if actual is None or route_key(actual) != route_key(expected):
                raise ValueError('ROUTE_CHANGED')
            selected[route_key(expected)] = entry['slot']
        current = time.time()
        self.inbox_snapshots = {k: v for k, v in self.inbox_snapshots.items() if v['expires'] > current}
        self.inbox_receipts = {k: v for k, v in self.inbox_receipts.items() if v['expires'] > current}
        op = args['op']
        if op == 'inbox_list':
            limit, offset = args.get('limit', 20), args.get('offset', 0)
            if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
                raise ValueError('INVALID_ARGUMENT')
            snapshot_id = args.get('snapshot')
            if snapshot_id:
                snapshot = self.inbox_snapshots.get(snapshot_id)
                if snapshot is None:
                    raise ValueError('CURSOR_EXPIRED')
                if (snapshot['owner'], snapshot['context']) != (owner, context):
                    raise ValueError('NOT_FOUND')
            else:
                if offset:
                    raise ValueError('INVALID_ARGUMENT')
                items = []
                for message in self.messages.values():
                    slot = selected.get(route_key(message['route']))
                    # Correlated replies are ALWAYS operation-only, even before an
                    # operation has saved its backend ID or observed its first reply.
                    if (slot is not None and message['reply_to'] is None
                            and 'close_request' not in message and message['state'] not in TERMINAL_STATES):
                        items.append(self.inbox_metadata(message, slot))
                items.sort(key=lambda m: (m['created_at'], m['id']))
                size = len(json.dumps(items, ensure_ascii=False).encode())
                if size > INBOX_SNAPSHOT_BYTES:
                    raise ValueError('CAPACITY_EXCEEDED')
                if any(len(json.dumps(item, ensure_ascii=False).encode()) > INBOX_PAGE_BYTES // 2 for item in items):
                    raise ValueError('INBOX_ITEM_TOO_LARGE')
                snapshot_id = str(uuid.uuid4())
                snapshot = {'owner': owner, 'context': context, 'items': items,
                            'expires': current + INBOX_TTL, 'bytes': size}
            if offset > len(snapshot['items']):
                raise ValueError('INVALID_ARGUMENT')
            page = []
            for item in snapshot['items'][offset:offset + limit]:
                if len(json.dumps(page + [item], ensure_ascii=False).encode()) > INBOX_PAGE_BYTES:
                    break
                page.append(item)
            end = offset + len(page)
            if end < len(snapshot['items']) and snapshot_id not in self.inbox_snapshots:
                if (len(self.inbox_snapshots) >= INBOX_MAX_SNAPSHOTS
                        or sum(v['bytes'] for v in self.inbox_snapshots.values()) + snapshot['bytes'] > INBOX_TOTAL_BYTES):
                    raise ValueError('CAPACITY_EXCEEDED')
                self.inbox_snapshots[snapshot_id] = snapshot
            return {'messages': page, 'snapshot': snapshot_id, 'offset': end,
                    'has_more': end < len(snapshot['items']), 'expires': snapshot['expires'],
                    'uninitialized_agent_session_ids': uninitialized,
                    'broker_instance': self.instance}
        mid = args['id']
        receipt_key = (owner, context, mid)
        receipt = self.inbox_receipts.get(receipt_key)
        if op == 'inbox_ack' and receipt is not None:
            return dict(receipt['result'])
        message = self.messages.get(mid)
        if message is None:
            raise ValueError('MESSAGE_UNAVAILABLE')
        slot = selected.get(route_key(message['route']))
        if slot is None or message['reply_to'] is not None or 'close_request' in message:
            raise ValueError('NOT_FOUND')
        if message.get('body_expired'):
            raise ValueError('MESSAGE_BODY_EXPIRED')
        if op == 'inbox_get':
            offset, count = args.get('offset', 0), args.get('max_chars', 8000)
            if type(offset) is not int or not 0 <= offset <= len(message['body']) or type(count) is not int or not 1 <= count <= 16000:
                raise ValueError('INVALID_ARGUMENT')
            result = self.inbox_metadata(message, slot)
            end = min(offset + count, len(message['body']))
            result.update(body=message['body'][offset:end], offset=offset, end=end,
                          has_more=end < len(message['body']), broker_instance=self.instance)
            # Check the actual envelope as well: oversized route metadata never
            # mutates a message or makes an unacknowledged payload unavailable.
            if len(wire({'result': result, 'instance': self.instance})) > INBOX_PAGE_BYTES:
                raise ValueError('INBOX_ITEM_TOO_LARGE')
            return result
        if op == 'inbox_reply':
            return self.reply_to_inbox(owner, message, args)
        if op != 'inbox_ack':
            raise ValueError('INVALID_ARGUMENT')
        if message['state'] in TERMINAL_STATES:
            raise ValueError('ACK_EXPIRED')
        if message['state'] in ('dispatching', 'uncertain'):
            raise ValueError('DELIVERY_UNKNOWN')
        if len(self.inbox_receipts) >= INBOX_MAX_RECEIPTS:
            raise ValueError('CAPACITY_EXCEEDED')
        result = {'id': mid, 'acknowledged': True, 'acknowledged_at': now(),
                  'broker_instance': self.instance, 'reconcile_until': current + INBOX_TTL}
        self.inbox_receipts[receipt_key] = {'result': result, 'expires': current + INBOX_TTL}
        message['acknowledged_at'] = result['acknowledged_at']
        self.status(message, 'done', 'Explicit MCP inbox acknowledgement')
        return dict(result)

    def reply_to_inbox(self, owner, parent, args):
        mid = parent['id']
        if ('provider_retry' in parent or any(record[1] == mid and key.startswith(PROVIDER_NOTICE_KEY_PREFIXES)
                                             for key, record in self.keys.items())):
            raise ValueError('NOT_FOUND')
        if parent['state'] in ('dispatching', 'uncertain'):
            raise ValueError('DELIVERY_UNKNOWN')
        destination = parent['return_route']
        transport = destination['transport']
        if transport not in ('realtime', 'codex', 'claude-peer', 'agy'):
            raise ValueError('UNSUPPORTED_TRANSPORT')
        if transport != 'realtime' and not destination.get('session'):
            raise ValueError('NOT_FOUND')
        body, key = args.get('body'), args.get('key')
        if (not isinstance(body, str) or not body.strip()
                or not isinstance(key, str) or not key.strip() or len(key) > 256):
            raise ValueError('INVALID_ARGUMENT')
        try:
            body_size = len(body.encode())
        except UnicodeEncodeError:
            raise ValueError('INVALID_ARGUMENT') from None
        if body_size > 32768:
            raise ValueError('INVALID_ARGUMENT')
        digest = hashlib.sha256(json.dumps([owner, key]).encode()).hexdigest()
        downstream_key = INBOX_REPLY_KEY_PREFIX + digest
        replayed = downstream_key in self.keys
        try:
            message = self.post({'body': body, 'key': downstream_key}, mid, inbox_reply=True)
        except IdempotencyConflict:
            raise ValueError('IDEMPOTENCY_CONFLICT') from None
        except ValueError as error:
            if str(error) in MESSAGE_CAPACITY_ERRORS:
                raise ValueError('CAPACITY_EXCEEDED') from None
            if str(error) == CLOSING_ROUTE_ERROR:
                raise ValueError('ROUTE_CHANGED') from None
            raise
        return {'message': public_message(message), 'replayed': replayed}

    def archive_correspondence(self, args, *, verified_close=False):
        expected = args['expected_route']
        key = (expected['project'], expected['name'])
        if self.endpoints.get(key) != expected:
            raise ValueError('Archive route is missing or changed')
        if not verified_close:
            self.require_open_route(expected)
        transport, sid, cwd = (expected.get(field) for field in ('transport', 'session', 'cwd'))
        if (transport not in ('codex', 'claude-peer', 'agy') or not isinstance(sid, str)
                or str(uuid.UUID(sid)) != sid or not cwd):
            raise ValueError('Archive cleanup requires a provider session UUID and workspace')

        records = self.messages | self.compact_messages
        selected, active, uncertain = set(), set(), set()
        for mid, message in records.items():
            if any(same_archive_endpoint(expected, message[field]) for field in ('route', 'return_route')):
                selected.add(mid)
            if message['state'] == 'dispatching' or self.waiters.get(message['reply_to']):
                active.add(mid)
            if self.waiters.get(mid):
                active.update((mid, message['reply_to']))
            control = message.get('provider_retry', {})
            if control.get('status') in ('reserved', 'accepted'):
                active.update((mid, control.get('source_message_id'), control.get('event_id')))
            elif control.get('status') == 'uncertain':
                uncertain.update((mid, control.get('source_message_id'), control.get('event_id')))
            if message['state'] == 'uncertain':
                uncertain.add(mid)
        protected = selected & (active | uncertain)
        if args.get('check_only') is True:
            if selected & active:
                raise ValueError('Archive cleanup blocked by an active delivery, provider continuation or wait')
            return dict(supported=True, matched=len(selected), retained=len(protected))
        if args.get('archived') is not True:
            raise ValueError('Archive cleanup requires confirmed provider archival')

        removed = selected - protected
        for mid in removed:
            self.messages.pop(mid, None)
            self.compact_messages.pop(mid, None)
            self.answers.pop(mid, None)
        self.keys = {key: value for key, value in self.keys.items() if value[1] not in removed}
        self.inbox_receipts = {key: value for key, value in self.inbox_receipts.items() if key[2] not in removed}
        # Invalidate the full snapshot so surviving pagination offsets stay stable.
        self.inbox_snapshots = {key: value for key, value in self.inbox_snapshots.items()
                                if not any(item['id'] in removed for item in value['items'])}
        result = dict(removed=len(removed), retained=len(protected), complete=not protected)
        if protected:
            result['active_retained'] = len(selected & active)
        return result

    def purge_archive_events(self, removed):
        if not removed:
            return

        def mark(event):
            if event.get('message', {}).get('id') in removed:
                event['_archive_purged'] = True
        for event in self.events:
            mark(event)
        self.events = deque((event for event in self.events if not event.get('_archive_purged')), maxlen=1024)
        for sub in self.subscribers:
            for event in sub.get('backlog', ()):
                mark(event)
            task = sub.get('next_event')
            if task is not None and task.done() and not task.cancelled() and task.exception() is None:
                mark(task.result())
            queued = []
            while not sub['queue'].empty():
                event = sub['queue'].get_nowait()
                mark(event)
                if not event.get('_archive_purged'):
                    queued.append(event)
            for event in queued:
                sub['queue'].put_nowait(event)

    def action(self, args, *, verified_close=False):
        # Synchronous entry point for standalone State use. The serving broker
        # admits mutations through action_async or an explicitly serialized batch.
        result, staged = self.stage_action(args, verified_close=verified_close)
        if staged is not None:
            self.correspondence.save(staged)
            self.publish_action(staged, args['op'])
        return result

    async def action_async(self, args, *, admit=None):
        if args['op'] in READ_ONLY_OPS:
            return self.apply_action(args)
        return await self.serialized(self.commit_action, args, admit=admit)

    async def serialized(self, operation, *args, admit=None):
        async def run_admitted():
            if admit is not None:
                admit()
            return await operation(*args)

        await self.mutation_lock.acquire()
        try:
            if self.mutations_closed:
                raise ValueError('Broker is stopping')
            task = asyncio.create_task(run_admitted())
        except BaseException:
            self.mutation_lock.release()
            raise
        self.active_mutation = task
        task.add_done_callback(self.finish_mutation)
        # The admitted task owns the lock. A disconnected or cancelled caller
        # cannot release it while a worker can still replace the checkpoint.
        return await asyncio.shield(task)

    def finish_mutation(self, task):
        self.active_mutation = None
        self.mutation_lock.release()
        if not task.cancelled():
            task.exception()  # The caller may have disconnected.

    async def drain_mutations(self):
        self.mutations_closed = True
        if self.active_mutation is not None:
            await asyncio.gather(asyncio.shield(self.active_mutation), return_exceptions=True)

    async def commit_action(self, args, *, verified_close=False):
        result, staged = self.stage_action(args, verified_close=verified_close)
        if staged is not None:
            # These maps are private to the stage. Give the worker the
            # checkpoint data and a store copy, with no live asyncio objects.
            checkpoint = Checkpoint(*(getattr(staged, field) for field in CHECKPOINT_FIELDS))
            store = copy(self.correspondence)
            try:
                await asyncio.get_running_loop().run_in_executor(self.checkpoint_executor, store.save, checkpoint)
            finally:
                self.correspondence.update_save_status(store)
            self.publish_action(staged, args['op'])
        return result

    def stage_action(self, args, *, verified_close=False):
        # Stage mutations in isolation. A failed durable write must not acknowledge
        # an ACK, publish an event, wake a waiter or leave a half-posted reply.
        mutations = ('send', 'send-with-retention', 'reply', 'ack', 'progress', 'resolve', 'claim', 'finish',
                     'claim-guarded', 'release-claim', 'note-pending',
                     'inbox_ack', 'inbox_reply', 'provider-failure', 'provider-retry', 'retention-sweep',
                     'archive-correspondence', 'close-dispatch', 'close-operator')
        archive = args['op'] == 'archive-correspondence'
        if archive and args.get('check_only') is True:
            return self.archive_correspondence(args), None
        planned = None
        planned_at = None
        if args['op'] == 'retention-sweep':
            if not self.retention_ttl and not self.retention_prepare:
                return {'changed': False}, None
            planned_at = time.time()
            planned = self.retention_plan(planned_at)
            if not self.retention_changes(planned):
                return {'changed': False}, None  # No deep copy or checkpoint write on an idle tick.
        if self.correspondence is None or args['op'] not in mutations:
            if planned is not None:
                return {'changed': self.sweep_retention(planned_at, planned=planned)}, None
            archive_before = (self.messages.keys() | self.compact_messages.keys()) if archive else set()
            result = self.apply_action(args, verified_close=verified_close)
            if archive:
                self.purge_archive_events(archive_before - (self.messages.keys() | self.compact_messages.keys()))
            return result, None
        staged = copy(self)
        for field in CHECKPOINT_FIELDS:
            setattr(staged, field, deepcopy(getattr(self, field)))
        staged.answers = self.answers.copy()
        staged.pending_publications = []
        result = ({'changed': staged.sweep_retention(planned_at, planned=planned)}
                  if planned is not None else staged.apply_action(args, verified_close=verified_close))
        if args['op'] == 'retention-sweep' and not result['changed']:
            return result, None
        if (args['op'] in ('inbox_reply', 'provider-failure', 'provider-retry', 'release-claim', 'note-pending')
                and not staged.pending_publications):
            return result, None  # A no-op needs no repeated checkpoint write.
        if archive and not result['removed']:
            return result, None
        return result, staged

    def publish_action(self, staged, op):
        archive = op == 'archive-correspondence'
        archive_before = (self.messages.keys() | self.compact_messages.keys()) if archive else set()
        for field in (*CHECKPOINT_FIELDS, 'answers'):
            setattr(self, field, getattr(staged, field))
        if archive:
            self.purge_archive_events(archive_before - (self.messages.keys() | self.compact_messages.keys()))
        for message, kind in staged.pending_publications:
            self.publish(message, kind)
            if message['state'] == 'answered' or ('close_request' in message and message['state'] == 'done'):
                self.answers[message['id']].set()

    def provider_failure(self, origin, source_message_id, failure):
        key = (origin['project'], origin['name'])
        current = self.endpoints.get(key)
        self.require_open_route(origin)
        source = codex_failures.last_sender_message(self, origin)
        if (current != origin or origin['transport'] not in ('codex', 'agy')
                or (source['id'] if source else None) != source_message_id):
            raise ValueError('Provider observation route or task changed')
        destination = source['return_route'] if source else origin
        transport = origin['transport']
        if not isinstance(failure, dict):
            raise ValueError('Invalid normalized provider failure')
        if transport == 'codex':
            if (set(failure) != {'kind', 'failed_at', 'turn_id'}
                    or codex_failures.transient_kind(failure['kind']) is None
                    and failure['kind'] != 'httpConnectionFailed'):
                raise ValueError('Invalid normalized provider failure')
            if not isinstance(failure['turn_id'], str) or not 1 <= len(failure['turn_id']) <= 256:
                raise ValueError('Invalid provider failure turn')
            identity = [origin['project'], origin['name'], origin['session'], failure['turn_id'],
                        route_key(destination)]
            extra = {}
        else:
            if (set(failure) != {'kind', 'failed_at', 'turn_id', 'error_id'}
                    or not agy_live.valid_failure_kind(failure['kind'])):
                raise ValueError('Invalid normalized provider failure')
            if (not isinstance(failure['turn_id'], str) or not re.fullmatch(r'agy:\d+', failure['turn_id'])
                    or len(failure['turn_id']) > 256):
                raise ValueError('Invalid provider failure turn')
            error_id = failure['error_id']
            if not isinstance(error_id, str) or not 1 <= len(error_id) <= 128 or any(c.isspace() for c in error_id):
                raise ValueError('Invalid provider failure error ID')
            identity = [origin['project'], origin['name'], origin['session'], failure['turn_id'],
                        error_id, route_key(destination)]
            extra = dict(error_id=error_id)
        failed_at = failure['failed_at']
        if failed_at is not None:
            parsed = datetime.fromisoformat(failed_at)
            if parsed.tzinfo is None or len(failed_at) > 40:
                raise ValueError('Invalid provider failure time')
        dedup_key = provider_failures.DETECTED_EVENT + ':' + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if dedup_key in self.keys:
            return self.get(self.keys[dedup_key][1])
        observed_at = now()
        source_label = ('turn_duration' if transport == 'codex' else 'step_metadata') if failed_at else 'unavailable'
        body = json.dumps(dict(event=provider_failures.DETECTED_EVENT, provider=transport,
                               kind=failure['kind'], failed_at=failed_at,
                               failure_time_source=source_label,
                               observed_at=observed_at, turn_id=failure['turn_id'],
                               source_message_id=source_message_id, **extra), sort_keys=True)
        return self.provider_notice(origin, destination, body, dedup_key,
                                    provider_failures.DETECTED_EVENT, protect=source_message_id)

    def provider_notice(self, origin, destination, body, dedup_key, event, *, conversation=None, protect=None):
        if dedup_key in self.keys:
            return self.get(self.keys[dedup_key][1])
        self.make_room(protect)
        mid = str(uuid.uuid4())
        notice = dict(id=mid, conversation=conversation or str(uuid.uuid4()), project=destination['project'],
                      sender=origin['name'], recipient=destination['name'], route=deepcopy(destination),
                      return_route=deepcopy(origin), channel=destination.get('channel'),
                      sender_channel=origin.get('channel'), body=body, reply_to=None,
                      state='pending', created=now(), events=[], note=None)
        self.messages[mid] = notice
        intent = dict(project=notice['project'], sender=notice['sender'], recipient=notice['recipient'],
                      channel=notice['channel'], sender_channel=notice['sender_channel'], body=body,
                      key=dedup_key, reply_to=None)
        self.keys[dedup_key] = (intent, mid)
        self.answers[mid] = asyncio.Event()
        self.status(notice, 'pending', event=event)
        return notice

    def apply_action(self, args, *, verified_close=False):
        op = args['op']
        if op == 'archive-correspondence':
            return self.archive_correspondence(args, verified_close=verified_close)
        if op == 'close-prepare':
            return self.prepare_close(args)
        if op == 'close-dispatch':
            return self.dispatch_close(args)
        if op == 'close-operator':
            return self.claim_close(args)
        if op == 'retention-plan':
            return dict(self.retention_plan()['counts'], dry_run=True, live_waiters_visible=True,
                        active_waits=sum(self.waiters.values()), retention_ttl_enabled=self.retention_ttl,
                        retention_prepare_enabled=self.retention_prepare,
                        retention_mode='ttl' if self.retention_ttl else 'prepare' if self.retention_prepare else 'legacy')
        if op == 'retention-sweep':
            return {'changed': self.sweep_retention()}
        if op == 'provider-failure':
            return public_message(self.provider_failure(args['expected_route'], args['source_message_id'], args['failure']))
        if op == 'provider-retry':
            return public_message(codex_failures.retry_action(self, args))
        if op in ('inbox_list', 'inbox_get', 'inbox_ack', 'inbox_reply'):
            return self.inbox_action(args)
        if op == 'ping':
            return {'instance':self.instance,'build':self.build,
                    'messages':len(self.messages),'subscribers':len(self.subscribers),
                    'correspondence_lost': bool(self.correspondence and self.correspondence.loss),
                    'correspondence_over_limit': len(self.messages) > self.max_messages,
                    'retention_ttl_enabled': self.retention_ttl,
                    'retention_prepare_enabled': self.retention_prepare, 'retention_protocol': 1,
                    'compact_receipts': len(self.compact_messages),
                    'checkpoint_last_save_seconds': self.correspondence.last_save_seconds if self.correspondence else None,
                    'checkpoint_max_save_seconds': self.correspondence.max_save_seconds if self.correspondence else 0.0,
                    'checkpoint_bytes': self.correspondence.checkpoint_bytes if self.correspondence else 0,
                    'max_messages': self.max_messages}
        if op == 'register':
            project, name = identifier(args['project']), identifier(args['name'])
            transport = args.get('transport','realtime')
            if transport not in ('realtime','codex','claude','claude-peer','agy'):
                raise ValueError('Unknown transport')
            cwd = str(Path(args['cwd']).expanduser().resolve(strict=True))
            if not Path(cwd).is_dir():
                raise ValueError('Working directory must be a directory')
            sid = args.get('session')
            if transport != 'realtime' or sid:
                sid = str(uuid.UUID(sid or ''))
            executable = args.get('executable')
            if executable is not None and not isinstance(executable, str):
                raise ValueError('Executable must be a string')
            item = dict(project=project,name=name,transport=transport,cwd=cwd,
                        session=sid,executable=executable)
            self.require_open_route(item)
            current = self.endpoints.get((project, name))
            if current is not None:
                self.require_open_route(current)
                # One event-loop turn: this authoritative check precedes every
                # routing write, regardless of any caller's read-only preflight.
                if (any(current.get(field) != item[field] for field in ('transport', 'cwd', 'session'))
                        or executable is not None and current.get('executable') != executable):
                    raise ValueError(REGISTER_CONFLICT_ERROR)
                # An omitted executable never clears an execution-bearing value.
                return current
            return self.routing_change('endpoints', (project,name), item)
        if op == 'routes':
            return [x for (p,_),x in self.endpoints.items() if p==args['project']]
        if op == 'codex-routes':
            return [route for route in self.endpoints.values()
                    if route['transport'] == 'codex' and 'closing' not in route]
        if op == 'close-prepare':
            return self.prepare_close(args)
        if op == 'close-dispatch':
            return self.dispatch_close(args)
        if op == 'close-operator':
            return self.claim_close(args)
        if op in ('unregister', 'unregister-if-current', 'unregister-archived'):
            key = (args['project'], args['name'])
            current = self.endpoints.get(key)
            # A distinct wire operation prevents an older broker from ignoring
            # the comparison and removing a replacement endpoint by name.
            if op == 'unregister-archived' and (args.get('archived') is not True
                    or current != args.get('expected_route')):
                raise ValueError('Confirmed archive endpoint is missing or changed')
            if current is None:
                raise ValueError(f'No participant {args["name"]!r} is registered in project {args["project"]!r}')
            self.require_open_route(current)
            if op == 'unregister-if-current' and current != args['expected_route']:
                raise ValueError('Endpoint changed during close; its current route was kept')
            owned = sorted(name for (project,name),item in self.channels.items()
                           if project==args['project'] and item.get('active') and item['owner']==args['name'])
            if owned:
                raise ValueError(f'{args["name"]!r} owns active channel(s) {", ".join(owned)}; close or hand them off first')
            return self.routing_remove('endpoints', key)
        if op == 'channel-bind':
            project, name = args['project'], identifier(args['name'])
            existing = self.channels.get((project,name))
            generation = existing['generation'] if existing else 0
            if args.get('expect_generation',0) != generation:
                raise ValueError(f'Channel generation changed; current generation is {generation}')
            if not args['description'].strip():
                raise ValueError('Channel description is required')
            route = self.route(project,args['owner'])
            route.update(channel=name,generation=generation+1)
            item = dict(project=project,name=name,owner=args['owner'],description=args['description'],
                        route=route,generation=generation+1,active=True)
            return self.routing_change('channels', (project,name), item)
        if op == 'channels':
            result = []
            for (project,_),item in self.channels.items():
                if project==args['project']:
                    result.append({**item,'subscribers':sum(sub['route']==route_key(item['route']) for sub in self.subscribers)})
            return result
        if op == 'channel-close':
            item = self.channels.get((args['project'],args['name']))
            if not item or item['generation']!=args['expect_generation']:
                raise ValueError('Channel missing or generation changed')
            item = dict(item, active=False, generation=item['generation']+1)
            return self.routing_change('channels', (args['project'],args['name']), item)
        if op in ('send','send-with-retention','reply'):
            return public_message(self.post(args, args.get('id') if op=='reply' else None))
        if op == 'show':
            message = public_message(self.get(args['id']))
            if message['state'] == 'pending':
                return message | self.pending_delivery(message, self.delivery_blockers())
            return message
        if op == 'conversation':
            cid = self.get(args['id'])['conversation']
            return [public_message(m) for m in (self.messages | self.compact_messages).values() if m['conversation']==cid]
        if op == 'inbox':
            if not args.get('name') and not args.get('channel'):
                raise ValueError('Select a recipient or channel')
            blockers = self.delivery_blockers()
            return [{k:m[k] for k in ('id','sender','recipient','state','channel','sender_channel','reply_to','created')}
                    | {'session':m['route']['session'],'generation':m['route'].get('generation')}
                    | self.pending_delivery(m, blockers)
                    for m in self.messages.values() if m['project']==args['project']
                    and (not args.get('name') or m['recipient']==args['name'])
                    and (not args.get('channel') or m['channel']==args['channel'])
                    and (not args.get('session') or m['route']['session']==args['session'])
                    and (args.get('all') or m['state'] not in ('done','answered'))]
        if op in ('ack','progress','resolve','claim','claim-guarded','release-claim','note-pending','finish'):
            m = self.get(args['id'])
            if m.get('body_expired'):
                raise ValueError('Message body expired; only lookup and correlated replies remain available')
            if op == 'note-pending':
                # Annotate a message only while it is still pending: never rewrite a claim
                # another caller took meanwhile, and never change its state.
                note = args.get('note')
                if not isinstance(note, str) or not note.strip():
                    raise ValueError('A pending note requires an explanation')
                if m['state'] != 'pending':
                    return {'noted':False}
                self.status(m, 'pending', note)
                return {'noted':True}
            if op == 'release-claim':
                token, admit_until = guarded_claim_fields(args)
                if time.monotonic() < admit_until:
                    raise ValueError('Delivery claim admission is still open')
                if (m['state'] != 'dispatching' or m.get('dispatch_claim') != token
                        or m.get('dispatch_claim_deadline') != admit_until):
                    return {'released':False}
                self.status(m, 'pending', CLAIM_RELEASE_NOTE, 'push_failed')
                return {'released':True}
            if op in ('claim', 'claim-guarded'):
                if retention.request_status(m, time.time()).get('request_status') == 'expired':
                    raise ValueError('Request reply deadline expired; delivery is not retried')
                if m['state']!='pending':
                    raise ValueError('Only pending messages may be delivered')
                self.require_open_route(m['route'])
                self.require_open_route(m['return_route'])
                if 'close_request' in m:
                    if args.get('close_id') != m['close_request']['close_id']:
                        raise ValueError('A close handoff cannot be delivered manually')
                    self.check_close_operator(m)
                destination = delivery_destination(m['route'])
                for other in self.messages.values():
                    if (other.get('provider_retry', {}).get('status') == 'reserved'
                            and delivery_destination(other['return_route']) == destination):
                        raise ValueError('Recipient has a reserved provider continuation')
                blocker = self.delivery_blockers().get(destination)
                if blocker:
                    note = self.delivery_wait(blocker)['note']
                    raise ValueError(f"Recipient has an active or uncertain delivery. Message {m['id']} {note}")
                if op == 'claim-guarded':
                    token, admit_until = guarded_claim_fields(args)
                    if time.monotonic() >= admit_until:
                        raise ValueError('Delivery claim admission expired')
                    self.status(m, 'dispatching')
                    m.update(dispatch_claim=token, dispatch_claim_deadline=admit_until)
                    return {'dispatch_claim':token}
                self.status(m,'dispatching')
                if args.get('guarded') is True:
                    m['dispatch_claim'] = str(uuid.uuid4())
                    return {'dispatch_claim':m['dispatch_claim']}
                return m
            if op=='finish':
                if m['state'] not in ('dispatching', 'pending', 'uncertain'):
                    return m
                if args.get('expect_dispatch_claim') != m.get('dispatch_claim'):
                    raise ValueError('Delivery claim changed. Inspect the current state.')
                if m['state']!='dispatching':
                    return m
                if args['state'] not in ('submitted','failed','uncertain','pending'):
                    raise ValueError('Invalid delivery result')
                event = args.get('event')
                if event not in (None,'pushed','push_failed'):
                    raise ValueError('Invalid delivery event')
                if args['state']=='pending' and (event!='push_failed' or not args.get('note','').strip()):
                    # A failed push stays deliverable, but never silently.
                    raise ValueError('Returning to pending requires a push_failed event and reason')
                reason = args.get('reason')
                if reason is not None and (reason != message_store.ARCHIVED_REASON
                        or args['state'] != 'failed' or m['route']['transport'] != 'codex'):
                    raise ValueError('Invalid delivery reason')
                # Keep the normalized wake outcome on the message before it is published (#121).
                peer_wake.record_wake(m, args.get('wake'))
                return self.status(m,args['state'],args.get('note'),event,reason)
            if m['state'] in ('done','answered'):
                raise ValueError('Message is already complete')
            if 'close_request' in m and (op != 'resolve' or args.get('state') != 'done'
                    or self.close_target(m['close_request']) is not None):
                raise ValueError('Codex close handoffs use native archive verification')
            if op in ('ack','progress') and m['state'] in ('dispatching','uncertain'):
                raise ValueError('Resolve delivery outcome first')
            state = {'ack':'received','progress':'in_progress'}.get(op,args.get('state'))
            if op=='resolve' and (state not in ('pending','submitted','done') or not args.get('note','').strip()):
                raise ValueError('Recovery requires a supported state and explanation')
            if state not in TERMINAL_STATES:
                self.require_open_route(m['route'])
                self.require_open_route(m['return_route'])
                if m.get('reason') == message_store.ARCHIVED_REASON:
                    raise ValueError('Archived delivery is terminal')
            return self.status(m,state,args.get('note'))
        raise ValueError('Unknown operation')

    def prepare_close(self, args):
        key = (identifier(args['project']), identifier(args['name']))
        current = self.endpoints.get(key)
        if not current or route_store.route_record(current) != args['expected_route']:
            raise ValueError('Endpoint is missing or changed')
        if current['transport'] != 'codex':
            raise ValueError('Close handoff requires a Codex endpoint')
        if 'closing' in current:
            return {'route': current, 'message': self.close_message(current['closing'])}
        operator = self.close_operator()
        if operator is not None and same_codex_thread(current, operator['route']):
            raise ValueError('The Codex close operator cannot archive itself')
        if any(other_key != key and same_codex_thread(current, item)
               for other_key, item in self.endpoints.items()):
            raise ValueError('This Codex thread has another registered route; detach that alias before closing')
        if any(channel['active'] and ((channel['project'], channel['owner']) == key
                                     or same_codex_thread(current, channel['route']))
               for channel in self.channels.values()):
            raise ValueError('Close or hand off active channels before closing this Codex thread')
        self.archive_correspondence({'expected_route': current, 'check_only': True})
        for message in self.messages.values():
            if message['state'] in TERMINAL_STATES:
                continue
            endpoints = (message['route'], message['return_route'])
            if message['state'] == 'uncertain' and any(same_archive_endpoint(current, item) for item in endpoints):
                continue  # Archive cleanup retains it and its idempotency key.
            if any(same_codex_thread(current, item) for item in endpoints):
                raise ValueError('This Codex thread has unfinished correspondence; reconcile it before closing')
        route = self.routing_change('endpoints', key, dict(current, closing=str(uuid.uuid4())))
        return {'route': route, 'operator': operator}

    def close_operator(self):
        if self.store is None:
            return None
        path = self.store.directory / CLOSE_OPERATOR_FILE
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise ValueError('Codex close operator configuration is unavailable') from None
        try:
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('operator configuration is not owner-private')
                settings = json.loads(source.read(4097)) if info.st_size <= 4096 else None
            if not isinstance(settings, dict) or set(settings) != {'project', 'name', 'session', 'sender'}:
                raise ValueError('invalid operator configuration fields')
            project = identifier(settings['project'])
            route = self.route(project, identifier(settings['name']))
            sender = self.route(project, identifier(settings['sender']))
            if (route['transport'] != 'codex' or route['session'] != str(uuid.UUID(settings['session']))
                    or sender['transport'] != 'realtime'):
                raise ValueError('operator or sender route differs')
            return {'route': route, 'sender': sender}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ValueError('Codex close operator configuration or enrolled route is invalid') from None

    def close_message(self, close_id):
        entry = self.keys.get(CLOSE_KEY_PREFIX + close_id)
        if entry is None:
            return None
        message = self.get(entry[1])
        if message.get('close_request', {}).get('close_id') != close_id:
            raise ValueError('Codex close key conflicts with existing correspondence')
        return message

    def close_target(self, request):
        target = request['target']
        current = self.endpoints.get((target['project'], target['name']))
        if current == {**target, 'closing': request['close_id']}:
            return current
        return None

    def dispatch_close(self, args):
        target = self.closing_route(args)
        close_id = target['closing']
        previous = self.close_message(close_id)
        if previous is not None:
            return {'message': previous, 'created': False}
        operator = self.close_operator()
        if operator is None or operator != args['operator'] or same_codex_thread(target, operator['route']):
            raise ValueError('Enrolled Codex close operator changed; closing route kept')
        destination, sender = operator['route'], operator['sender']
        message = self.post({'project': destination['project'], 'recipient': destination['name'],
                             'sender': sender['name'], 'key': CLOSE_KEY_PREFIX + close_id,
                             'body': 'Verify this Agent Bridge native archive request using close-operator.'},
                            close_request={'close_id': close_id, 'target': route_store.route_record(target)})
        return {'message': message, 'created': True}

    def check_close_operator(self, message):
        request = message['close_request']
        if self.close_message(request['close_id']) != message or self.close_target(request) is None:
            raise ValueError('Codex close request does not match a closing route')
        operator = self.close_operator()
        if operator != {'route': message['route'], 'sender': message['return_route']}:
            raise ValueError('Enrolled Codex close operator changed; closing route kept')
        return request

    def claim_close(self, args):
        message = self.get(args['id'])
        if 'close_request' not in message or message['route']['session'] != args['operator_session']:
            raise ValueError('This Codex chat is not the enrolled close operator for the request')
        request = self.check_close_operator(message)
        if message['state'] not in ('dispatching', 'submitted', 'uncertain'):
            raise ValueError('Codex close request cannot be claimed again')
        self.status(message, 'in_progress', 'Enrolled Codex operator accepted the native archive handoff')
        return request

    def closing_route(self, args):
        key = (identifier(args['project']), identifier(args['name']))
        current = self.endpoints.get(key)
        if (not current or 'closing' not in current or current['closing'] != args['close_id']
                or current['session'] != args['session']):
            raise ValueError('Codex close generation or session does not match the closing route')
        return current


async def finish_close(state, args):
    route = state.closing_route(args)
    timeout = args.get('timeout', 10)
    if not 0 < timeout <= 600:
        raise ValueError('Timeout must be between 0 and 600 seconds')
    try:
        await asyncio.wait_for(asyncio.to_thread(codex_preview.verify_archive, route), timeout)
    except asyncio.TimeoutError:
        raise ValueError('Codex archive verification timed out; closing route kept') from None
    return await state.serialized(commit_close, state, args, route)


async def commit_close(state, args, route):
    if state.closing_route(args) != route:
        raise ValueError('Endpoint changed during archive verification')

    result = {**route, 'route_removed': False}
    try:
        cleanup = await state.commit_action(
            {'op': 'archive-correspondence', 'expected_route': route, 'archived': True}, verified_close=True)
    except (ValueError, OSError) as error:
        return {**result, 'cleanup_error': str(error),
                'correspondence_cleanup': {'confirmed': False, 'state': 'rejected'}}
    result['correspondence_cleanup'] = cleanup
    if cleanup.get('active_retained'):
        result['cleanup_error'] = 'Archive confirmed; active correspondence remains'

    message = state.close_message(route['closing'])
    try:
        state.routing_remove('endpoints', (route['project'], route['name']))
    except (ValueError, OSError) as error:
        result['route_error'] = str(error)
        result.setdefault('cleanup_error', 'Archive confirmed; route removal was not confirmed')
        return result
    result['route_removed'] = True
    if message is not None:
        try:
            await state.commit_action({'op': 'resolve', 'id': message['id'], 'state': 'done',
                                       'note': 'Native Codex archive verified and route removed'})
            result['operator_receipt'] = 'done'
        except (OSError, ValueError):
            result['operator_receipt'] = 'needs_reconciliation'
            result['operator_error'] = 'Archive confirmed; close operator receipt needs reconciliation'
    return result


async def write(writer, payload):
    writer.write(wire(payload))
    await asyncio.wait_for(writer.drain(),5)


async def wait_answer(state, args, reader):
    mid = args['id']
    async with state.mutation_lock:
        message = state.get(mid)
        timeout = args.get('timeout',60)
        if not 0 < timeout <= 3600:
            raise ValueError('Timeout must be between 0 and 3600 seconds')
        state.waiters[mid] = state.waiters.get(mid, 0) + 1
        pending = [asyncio.create_task(state.answers[mid].wait()),asyncio.create_task(reader.read(1))]
    try:
        done,_ = await asyncio.wait(pending,timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
        if pending[1] in done:
            return None
        if 'close_request' in message:
            current = state.get(mid)['state']
            return {'id': mid, 'timed_out': current != 'done', 'close_status': current, 'replies': []}
        replies = [public_message(m) for m in state.messages.values() if m['reply_to']==mid]
        return ({'id':mid,'timed_out':not bool(replies),'replies':replies}
                | ({'body_expired': True} if message.get('body_expired') else {}))
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        state.waiters[mid] -= 1
        if not state.waiters[mid]:
            del state.waiters[mid]


async def watch(state, args, reader, writer):
    if len(state.subscribers)>=256:
        raise ValueError('Subscriber limit reached')
    route = state.route(args['project'],args.get('name'),args.get('channel'))
    cursor = args.get('after')
    if cursor is not None:
        if args.get('instance') != state.instance:
            raise ValueError('Broker instance changed; register and resynchronize')
        if cursor>state.sequence or (state.events and cursor<state.events[0]['seq']-1):
            raise ValueError('Event cursor is outside retained history; inspect inbox')
    sub = {'route':route_key(route),'queue':asyncio.Queue(maxsize=128),'overflow':False}
    backlog = [event for event in state.events if cursor is not None and event['seq']>cursor and state.matches(sub,event)]
    sub['backlog'] = backlog
    state.subscribers.append(sub)  # Register before yielding to avoid a subscribe/send race.
    disconnected = asyncio.create_task(reader.read(1))
    try:
        await write(writer,{'type':'subscribed','instance':state.instance,'seq':state.sequence,'route':route})
        for event in backlog:
            if not event.get('_archive_purged'):
                await write(writer,event)
        sub['backlog'] = []
        while True:
            next_event = asyncio.create_task(sub['queue'].get())
            sub['next_event'] = next_event
            try:
                done,_ = await asyncio.wait([disconnected,next_event],return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    return
                event = next_event.result()
                if event.get('_archive_purged'):
                    continue
                await write(writer,event)
                if 'error' in event:
                    return
            finally:
                next_event.cancel()
                await asyncio.gather(next_event,return_exceptions=True)
    finally:
        state.subscribers.remove(sub)
        disconnected.cancel()
        await asyncio.gather(disconnected,return_exceptions=True)


async def retention_loop(state):
    while True:
        await asyncio.sleep(60)
        try:
            await state.action_async({'op': 'retention-sweep'})
        except (ValueError, OSError):
            # Fixed count-only diagnostic; never reveal routes, bodies or IDs.
            print(json.dumps({'event': 'retention_sweep_failed'}), file=sys.stderr, flush=True)


async def serve(path, max_messages=2048, state_dir=None, usage_interval=usage.DEFAULT_INTERVAL_MINUTES,
                codex_executable='codex', claude_live=True,
                codex_failure_interval=codex_failures.DEFAULT_INTERVAL,
                retry_backoff=provider_failures.DEFAULT_BACKOFF, retry_cap=provider_failures.DEFAULT_CAP,
                agy_failure_interval=agy_live.DEFAULT_FAILURE_INTERVAL, retention_ttl=False, retention_prepare=False):
    retry_policy = provider_failures.retry_policy(retry_backoff, retry_cap)
    if type(codex_failure_interval) is not int or codex_failure_interval < 0:
        raise ValueError('Codex failure observation interval must be a nonnegative integer')
    if type(agy_failure_interval) is not int or agy_failure_interval < 0:
        raise ValueError('Antigravity failure observation interval must be a nonnegative integer')
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock = path.with_suffix('.lock').open('a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise ValueError('Broker is already running for this socket') from error
    server = None
    store = None
    checkpoint_executor = None
    state = None
    collectors = []
    failure_observer = None
    agy_failure_observer = None
    retention_task = None
    stopping = asyncio.Event()
    handlers = set()
    writers = set()
    try:
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise ValueError('Refusing to replace a non-socket file')
            path.unlink()
        store = route_store.RouteStore(state_dir or path.with_suffix('.state'))
        # A provider worker can wait for a durable reservation while using the
        # default executor. Keep checkpoint IO independent of those workers.
        checkpoint_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ab-checkpoint')
        state = State(max_messages, store, retention_ttl=retention_ttl, retention_prepare=retention_prepare,
                      checkpoint_executor=checkpoint_executor)
        if retention_ttl or retention_prepare:
            retention_task = asyncio.create_task(retention_loop(state))
        if codex_failure_interval:
            failure_observer = asyncio.create_task(
                codex_failures.observe_codex_failures(state, codex_failure_interval, retry_policy))
        if agy_failure_interval:
            agy_failure_observer = asyncio.create_task(
                agy_live.observe_agy_failures(state, agy_failure_interval, retry_policy))
        if usage_interval:
            # Read-only limit sampling beside the broker; it never touches `state`.
            collectors = [usage.Collector(usage.UsageStore(store.directory / 'usage', provider=provider),
                                          usage_interval, codex_executable,
                                          claude_live=claude_live).start()
                          for provider in usage.PROVIDERS]
        async def handle(reader,writer):
            if stopping.is_set():
                writer.transport.abort()
                return
            handlers.add(asyncio.current_task())
            writers.add(writer)
            try:
                raw = await asyncio.wait_for(reader.readline(),10)
                args = json.loads(raw)
                if not isinstance(args,dict):
                    raise ValueError('Request must be an object')
                op = args.get('op')
                if op=='watch':
                    await watch(state,args,reader,writer)
                elif op=='wait':
                    result = await wait_answer(state,args,reader)
                    if result is not None:
                        await write(writer,{'result':result,'instance':state.instance})
                elif op=='shutdown':
                    await write(writer,{'result':{'stopping':True},'instance':state.instance})
                    stopping.set()
                elif op=='close-finish':
                    result = await finish_close(state, args)
                    await write(writer,{'result':result,'instance':state.instance})
                else:
                    result = await state.action_async(args)
                    await write(writer,{'result':result,'instance':state.instance})
            except (ValueError,KeyError,TypeError,OSError,asyncio.TimeoutError) as error:
                try:
                    await write(writer,{'error':str(error),'instance':state.instance})
                except (OSError,asyncio.TimeoutError):
                    pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                handlers.discard(asyncio.current_task())
                writers.discard(writer)
        server = await asyncio.start_unix_server(handle,path=str(path),limit=MAX_FRAME)
        os.chmod(path,0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM):
            loop.add_signal_handler(sig,stopping.set)
        print(json.dumps({'ready':True,'socket':str(path),'instance':state.instance}),flush=True)
        await stopping.wait()
    finally:
        stopping.set()
        if server is not None:
            server.close()
        # Python 3.12.1+ waits for active clients in wait_closed(). Cancel
        # handlers and abort transports first, including stalled readers/writers,
        # then drain any admitted operation before releasing either store lock.
        active = list(handlers) + [task for task in (failure_observer, agy_failure_observer, retention_task)
                                   if task is not None]
        for writer in list(writers):
            writer.transport.abort()
        for task in active:
            task.cancel()

        async def close_resources():
            await asyncio.gather(*active, return_exceptions=True)
            if state is not None:
                await state.drain_mutations()
            if checkpoint_executor is not None:
                checkpoint_executor.shutdown()
            for collector in collectors:
                collector.stop()
            if server is not None:
                await server.wait_closed()
                path.unlink(missing_ok=True)
            if store is not None:
                store.close()
            lock.close()

        closing = asyncio.create_task(close_resources())
        interrupted = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                interrupted = True
        closing.result()
        if interrupted:
            raise asyncio.CancelledError


def broker_response(line):
    if not line:
        raise BrokerResponseError('Broker disconnected; delivery outcome may be unknown')
    if len(line)>MAX_FRAME:
        raise BrokerResponseError('Broker frame exceeds limit')
    try:
        result = json.loads(line)
    except ValueError:
        raise BrokerResponseError('Broker sent an unreadable response') from None
    if not isinstance(result, dict):
        raise BrokerResponseError('Broker sent an invalid response')
    if 'error' in result:
        raise ValueError(result['error'])
    return result


def stream(path, args, timeout=None):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(Path(path).expanduser()))
        except OSError as error:
            raise BrokerConnectError(error) from error
        sock.sendall(wire(args))
        with sock.makefile('rb') as reader:
            while True:
                yield broker_response(reader.readline(MAX_FRAME+1))


def request(path, args, timeout=10, *, deadline=None):
    if deadline is not None:
        return timed_request(path, args, deadline)
    connection = stream(path,args,timeout)
    try:
        return next(connection)['result']
    finally:
        connection.close()


def timed_request(path, args, deadline):
    """Read one broker reply against an absolute monotonic deadline."""
    def limit(sock):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Broker request timed out')
        sock.settimeout(remaining)

    payload = wire(args)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        limit(sock)
        try:
            sock.connect(str(Path(path).expanduser()))
        except OSError as error:
            raise BrokerConnectError(error) from error
        limit(sock)
        sock.sendall(payload)
        line = bytearray()
        while len(line) <= MAX_FRAME:
            limit(sock)
            chunk = sock.recv(min(65536, MAX_FRAME + 1 - len(line)))
            if not chunk:
                raise BrokerResponseError('Broker disconnected; delivery outcome may be unknown')
            end = chunk.find(b'\n')
            if end >= 0:
                line.extend(chunk[:end+1])
                response = broker_response(line)
                if 'result' not in response:
                    raise BrokerResponseError('Broker reply has no result')
                return response['result']
            line.extend(chunk)
        raise BrokerResponseError('Broker frame exceeds limit')
