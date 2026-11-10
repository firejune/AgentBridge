"""Single-process broker: durable routing and bounded correspondence."""
import asyncio
from collections import deque
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
import message_store
import usage
import provider_failures
import codex_failures
import agy_live

DEFAULT_SOCKET = Path.home() / '.local/share/agent-bridge/bridge.sock'
MAX_FRAME = 1024 * 1024
INBOX_TTL = 3600
INBOX_MAX_SNAPSHOTS = 16
INBOX_SNAPSHOT_BYTES = 4 * 1024 * 1024
INBOX_TOTAL_BYTES = 16 * 1024 * 1024
INBOX_PAGE_BYTES = 128 * 1024
INBOX_MAX_RECEIPTS = 8192


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


TERMINAL_STATES = frozenset(('done', 'answered', 'failed'))
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
    if 'dispatch_claim' not in message and 'dispatch_claim_deadline' not in message:
        return message
    return {key:value for key,value in message.items()
            if key not in ('dispatch_claim', 'dispatch_claim_deadline')}


class BrokerResponseError(ValueError):
    """The broker did not return a usable response to a request."""


class State:
    def __init__(self, max_messages=2048, store=None):
        if max_messages < 1:
            raise ValueError('Message retention size must be positive')
        self.instance = str(uuid.uuid4())
        self.max_messages = max_messages
        self.endpoints, self.channels, self.messages, self.keys = {}, {}, {}, {}
        self.events = deque(maxlen=1024)
        self.sequence = 0
        self.subscribers = []
        self.answers = {}
        self.waiters = {}
        self.inbox_snapshots = {}
        self.inbox_receipts = {}
        self.store = store
        if store is not None:
            self.endpoints, self.channels = store.load()
        self.correspondence = None
        self.pending_publications = None
        if store is not None:
            self.correspondence = message_store.MessageStore(store.directory, max_messages)
            saved = self.correspondence.load()
            self.messages = {m['id']: m for m in saved['messages']}
            self.keys = {key: (intent, mid) for key, intent, mid in saved['keys']}
            self.inbox_receipts = {tuple(key): value for key, value in saved['receipts']}
            self.inbox_snapshots = saved['snapshots']
            before = self.messages.copy()
            self.make_room(restoring=True)
            dropped = [message_store.loss_identity(m) for mid, m in before.items()
                       if mid not in self.messages]
            if dropped:
                self.correspondence.record_loss('message retention limit reduced; oldest unguarded terminal messages evicted', dropped)
            if len(self.messages) > self.max_messages:
                print(json.dumps(dict(correspondence_over_limit=self.correspondence.path.name,
                                      kept=len(self.messages), limit=self.max_messages)),
                      file=sys.stderr, flush=True)
            reply_parents = {m['reply_to'] for m in self.messages.values()}
            for mid, message in self.messages.items():
                self.answers[mid] = asyncio.Event()
                if mid in reply_parents:
                    self.answers[mid].set()
                if message['state'] == 'dispatching':
                    message.update(state='uncertain', note='Broker restarted during delivery; resolve explicitly')
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
                raise ValueError('In-memory message limit reached: no evictable terminal messages; '
                                 'pending or protected work is retained')
        for mid in evicted:
            del self.messages[mid]
            self.answers.pop(mid, None)
        self.keys = {key: value for key, value in self.keys.items() if value[1] in remaining}

    def get(self, mid):
        if mid not in self.messages:
            raise ValueError('Unknown message; broker may have restarted')
        return self.messages[mid]

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

    def route(self, project, name=None, channel=None):
        if channel:
            item = self.channels.get((project,channel))
            if not item or not item['active']:
                raise ValueError('Channel is missing or closed')
            return deepcopy(item['route'])
        item = self.endpoints.get((project,name))
        if not item:
            raise ValueError('Unregistered endpoint')
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

    def status(self, m, state, note=None, event=None):
        m.update(state=state,note=note)
        entry = {'state':state,'note':note,'at':now()}
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
        return m

    def post(self, args, reply_to=None):
        body, key = args['body'], args['key']
        if not isinstance(body,str) or not body.strip() or len(body.encode()) > 32768:
            raise ValueError('Message must contain 1–32768 UTF-8 bytes')
        if not isinstance(key,str) or not key.strip() or len(key)>256:
            raise ValueError('Provide an idempotency key of at most 256 characters')
        intent = {k:args.get(k) for k in ('project','sender','recipient','channel','sender_channel','body','key')}
        intent['reply_to'] = reply_to
        if key in self.keys:
            original_intent, mid = self.keys[key]
            if original_intent != intent:
                raise ValueError('Idempotency key already used with different content')
            return self.get(mid)
        if reply_to:
            parent = self.get(reply_to)
            destination, origin = deepcopy(parent['return_route']), deepcopy(parent['route'])
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
        self.messages[mid] = m
        self.keys[key] = (intent,mid)
        self.answers[mid] = asyncio.Event()
        self.publish(m,'message')
        if reply_to:
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
        # Runs in the same event-loop turn as route/epoch checks and ACK mutation.
        if args.get('instance') is not None and args['instance'] != self.instance:
            raise ValueError('BROKER_RESTARTED')
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
                    if slot is not None and message['reply_to'] is None and message['state'] not in TERMINAL_STATES:
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
        if slot is None or message['reply_to'] is not None:
            raise ValueError('NOT_FOUND')
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

    def action(self, args):
        # Stage mutations in isolation. A failed durable write must not acknowledge
        # an ACK, publish an event, wake a waiter or leave a half-posted reply.
        mutations = ('send', 'reply', 'ack', 'progress', 'resolve', 'claim', 'finish',
                     'claim-guarded', 'release-claim', 'note-pending',
                     'inbox_ack', 'provider-failure', 'provider-retry')
        if self.correspondence is None or args['op'] not in mutations:
            return self.apply_action(args)
        staged = copy(self)
        for field in ('messages', 'keys', 'inbox_receipts', 'inbox_snapshots'):
            setattr(staged, field, deepcopy(getattr(self, field)))
        staged.answers = self.answers.copy()
        staged.pending_publications = []
        result = staged.apply_action(args)
        if args['op'] in ('provider-failure', 'provider-retry', 'release-claim', 'note-pending') and not staged.pending_publications:
            return result  # A no-op needs no repeated checkpoint write.
        self.correspondence.save(staged)
        for field in ('messages', 'keys', 'inbox_receipts', 'inbox_snapshots', 'answers'):
            setattr(self, field, getattr(staged, field))
        for message, kind in staged.pending_publications:
            self.publish(message, kind)
            if message['state'] == 'answered':
                self.answers[message['id']].set()
        return result

    def provider_failure(self, origin, source_message_id, failure):
        key = (origin['project'], origin['name'])
        current = self.endpoints.get(key)
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

    def apply_action(self, args):
        op = args['op']
        if op == 'provider-failure':
            return public_message(self.provider_failure(args['expected_route'], args['source_message_id'], args['failure']))
        if op == 'provider-retry':
            return public_message(codex_failures.retry_action(self, args))
        if op in ('inbox_list', 'inbox_get', 'inbox_ack'):
            return self.inbox_action(args)
        if op == 'ping':
            return {'instance':self.instance,'messages':len(self.messages),'subscribers':len(self.subscribers),
                    'correspondence_lost': bool(self.correspondence and self.correspondence.loss),
                    'correspondence_over_limit': len(self.messages) > self.max_messages,
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
            item = dict(project=project,name=name,transport=transport,cwd=cwd,
                        session=sid,executable=args.get('executable'))
            return self.routing_change('endpoints', (project,name), item)
        if op == 'remap':
            key = (identifier(args['project']), identifier(args['name']))
            current = self.endpoints.get(key)
            if current is None or current != args['expected_route']:
                raise ValueError('Endpoint is missing or changed')
            if current['transport'] != 'codex':
                raise ValueError('Remap requires a Codex endpoint')
            sid = str(uuid.UUID(args['session']))
            if sid == current['session']:
                raise ValueError('Replacement UUID must differ from the current UUID')
            return self.routing_change('endpoints', key, dict(current, session=sid))
        if op == 'routes':
            return [x for (p,_),x in self.endpoints.items() if p==args['project']]
        if op == 'codex-routes':
            return [route for route in self.endpoints.values() if route['transport'] == 'codex']
        if op == 'unregister':
            key = (args['project'], args['name'])
            if key not in self.endpoints:
                raise ValueError(f'No participant {args["name"]!r} is registered in project {args["project"]!r}')
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
        if op in ('send','reply'):
            return public_message(self.post(args, args.get('id') if op=='reply' else None))
        if op == 'show':
            message = public_message(self.get(args['id']))
            if message['state'] == 'pending':
                return message | self.pending_delivery(message, self.delivery_blockers())
            return message
        if op == 'conversation':
            cid = self.get(args['id'])['conversation']
            return [public_message(m) for m in self.messages.values() if m['conversation']==cid]
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
                if m['state']!='pending':
                    raise ValueError('Only pending messages may be delivered')
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
                return self.status(m,args['state'],args.get('note'),event)
            if m['state'] in ('done','answered'):
                raise ValueError('Message is already complete')
            if op in ('ack','progress') and m['state'] in ('dispatching','uncertain'):
                raise ValueError('Resolve delivery outcome first')
            state = {'ack':'received','progress':'in_progress'}.get(op,args.get('state'))
            if op=='resolve' and (state not in ('pending','submitted','done') or not args.get('note','').strip()):
                raise ValueError('Recovery requires a supported state and explanation')
            return self.status(m,state,args.get('note'))
        raise ValueError('Unknown operation')


async def write(writer, payload):
    writer.write(wire(payload))
    await asyncio.wait_for(writer.drain(),5)


async def wait_answer(state, args, reader):
    mid = args['id']
    state.get(mid)
    timeout = args.get('timeout',60)
    if not 0 < timeout <= 3600:
        raise ValueError('Timeout must be between 0 and 3600 seconds')
    state.waiters[mid] = state.waiters.get(mid, 0) + 1
    pending = [asyncio.create_task(state.answers[mid].wait()),asyncio.create_task(reader.read(1))]
    try:
        done,_ = await asyncio.wait(pending,timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
        if pending[1] in done:
            return None
        replies = [public_message(m) for m in state.messages.values() if m['reply_to']==mid]
        return {'id':mid,'timed_out':not bool(replies),'replies':replies}
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
    state.subscribers.append(sub)  # Register before yielding to avoid a subscribe/send race.
    disconnected = asyncio.create_task(reader.read(1))
    try:
        await write(writer,{'type':'subscribed','instance':state.instance,'seq':state.sequence,'route':route})
        for event in backlog:
            await write(writer,event)
        while True:
            next_event = asyncio.create_task(sub['queue'].get())
            try:
                done,_ = await asyncio.wait([disconnected,next_event],return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    return
                event = next_event.result()
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


async def serve(path, max_messages=2048, state_dir=None, usage_interval=usage.DEFAULT_INTERVAL_MINUTES,
                codex_executable='codex', claude_live=True,
                codex_failure_interval=codex_failures.DEFAULT_INTERVAL,
                retry_backoff=provider_failures.DEFAULT_BACKOFF, retry_cap=provider_failures.DEFAULT_CAP,
                agy_failure_interval=agy_live.DEFAULT_FAILURE_INTERVAL):
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
    collectors = []
    failure_observer = None
    agy_failure_observer = None
    try:
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise ValueError('Refusing to replace a non-socket file')
            path.unlink()
        store = route_store.RouteStore(state_dir or path.with_suffix('.state'))
        state = State(max_messages, store)
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
        stopping = asyncio.Event()
        handlers = set()
        writers = set()
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
                else:
                    result = state.action(args)  # No await inside mutation: one atomic event-loop turn.
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
        server.close()
        # Python 3.12.1+ waits for active clients in wait_closed(). Cancel
        # handlers and abort transports first, including stalled readers/writers.
        active = list(handlers)
        for writer in list(writers):
            writer.transport.abort()
        for task in active:
            task.cancel()
        await asyncio.gather(*active,return_exceptions=True)
        await server.wait_closed()
    finally:
        for observer in (failure_observer, agy_failure_observer):
            if observer is not None:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
        for collector in collectors:
            collector.stop()
        if server is not None:
            server.close()
            path.unlink(missing_ok=True)
        if store is not None:
            store.close()
        lock.close()


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
            raise ValueError('Broker unavailable; start bridge.py serve for this socket') from error
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
            raise ValueError('Broker unavailable; start bridge.py serve for this socket') from error
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
