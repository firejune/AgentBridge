"""Opt-in owner-scoped MCP Events; subscriptions durable, arrivals never replayed.

Only webhook keys may enter Keychain. No credentials, signed inbox handles,
message bodies or event streams are written to the subscription metadata file.
"""
import base64
import copy
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
import queue
from pathlib import Path
import secrets
import select
import shlex
import socket
import stat
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit
from datetime import datetime, timezone

import broker

NAME = 'bridge.inbox.message.created'
MAX_SUBSCRIPTIONS = 64
DEFAULT_TTL_MS = 3600000
MAX_TTL_MS = 86400000
EVENT_METHODS = ('events/list', 'events/subscribe', 'events/unsubscribe')
REQUEST_STAGES = ('received', 'authorize', 'validate', 'resolve_routes',
    'verify_callback', 'start_watchers', 'reserve_key_reference', 'store_signing_key',
    'persist_subscription', 'persist_unsubscribe', 'cleanup_keys', 'complete')
REQUEST_REASONS = frozenset(('events_disabled', 'invalid_request', 'invalid_event',
    'invalid_arguments', 'invalid_delivery', 'invalid_callback_url',
    'invalid_signing_secret', 'replay_unavailable', 'invalid_lifetime',
    'subscription_capacity', 'non_public_callback', 'response_too_large',
    'callback_failed', 'challenge_failed', 'keychain_unavailable',
    'inbox_watch_unavailable', 'inbox_route_changed', 'owner_or_route_unavailable',
    'storage_failed', 'event_operation_failed'))
CATALOG = {'name': NAME, 'description': 'A new independent message arrived in your authorized Agent Bridge inbox. Read and explicitly acknowledge it using the inbox tools.',
    'delivery': ['webhook'], 'inputSchema': {'type': 'object', 'properties': {
        'agent_session_id': {'type': 'string'}}, 'additionalProperties': False},
    'payloadSchema': {'type': 'object', 'properties': {'agent_session_id': {'type': 'string'}},
        'required': ['agent_session_id'], 'additionalProperties': False}}


class EventError(Exception):
    def __init__(self, code, reason):
        self.code, self.reason = code, reason
        super().__init__(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')


def signing_key(value):
    try:
        if not isinstance(value, str) or len(value) > 100 or not value.startswith('whsec_'):
            raise ValueError()
        raw = base64.b64decode(value[6:], validate=True)
        if not 24 <= len(raw) <= 64:
            raise ValueError()
        return raw
    except (ValueError, TypeError):
        raise EventError(-32602, 'invalid_signing_secret') from None


class Keychain:
    """Dedicated MCP Events service; values never appear in process arguments."""
    SERVICE = 'agent-bridge.mcp-events'

    def _run(self, words, missing_ok=False):
        result = subprocess.run(['/usr/bin/security', '-i'],
            input=' '.join(shlex.quote(word) for word in words) + '\n',
            text=True, capture_output=True, timeout=10)
        # Interactive security can return zero while a command failed; stderr
        # carries the command status. Never include its output in exceptions.
        if result.returncode or any(marker in result.stderr for marker in ('SecKeychain', 'SecItem', 'security:', 'could not be found')):
            if missing_ok and 'could not be found' in result.stderr:
                return None
            raise EventError(-32603, 'keychain_unavailable')
        return result.stdout

    def put(self, account, value):
        self._run(['add-generic-password', '-U', '-s', self.SERVICE, '-a', account, '-w', value])

    def get(self, account):
        value = self._run(['find-generic-password', '-s', self.SERVICE, '-a', account, '-w'])
        # security -i adds its prompt outside the password line on some versions.
        for line in value.splitlines():
            if line.startswith('whsec_'):
                signing_key(line)
                return line
        raise EventError(-32603, 'keychain_unavailable')

    def delete(self, account):
        self._run(['delete-generic-password', '-s', self.SERVICE, '-a', account], missing_ok=True)


class HTTPSCallback:
    """Resolve once per request; connect to a validated IP with original TLS SNI."""
    def __call__(self, url, body, headers):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443) or len(url) > 4096
                or any(ord(char) < 33 or ord(char) == 127 for char in url)):
            raise EventError(-32015, 'invalid_callback_url')
        host = parsed.hostname
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global or ipaddress.ip_address(item[4][0]).is_multicast for item in addresses):
                raise EventError(-32015, 'non_public_callback')
            family, kind, proto, _, destination = addresses[0]
            raw = socket.socket(family, kind, proto)
            raw.settimeout(10)
            try:
                raw.connect(destination)
                conn = http.client.HTTPSConnection(host, 443, timeout=10)
                conn.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
                try:
                    target = parsed.path or '/'
                    if parsed.query:
                        target += '?' + parsed.query
                    conn.request('POST', target, body=body, headers=headers)
                    response = conn.getresponse()
                    data = response.read(65537)
                    if len(data) > 65536:
                        raise EventError(-32015, 'response_too_large')
                    return response.status, data
                finally:
                    conn.close()
            finally:
                raw.close()
        except EventError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise EventError(-32015, 'callback_failed') from None


class ArrivalAudit:
    """Two bounded owner-private files; fixed receipt metadata only."""
    METHODS = ('initialize', 'notifications/initialized', 'notifications/cancelled',
        'server/discover', 'resources/list', 'prompts/list', 'ping', 'tools/list',
        'tools/call', *EVENT_METHODS, 'other')
    PROTOCOLS = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25', '2026-07-28')
    TOOLS = ('bridge_status', 'bridge_sessions_list', 'bridge_session_create',
        'bridge_message_send', 'bridge_operation_get', 'bridge_inbox_list',
        'bridge_message_get', 'bridge_message_ack', 'other')

    def __init__(self, root, max_bytes=1024 * 1024):
        self.path = Path(root) / 'events-audit.jsonl'
        self.previous = Path(root) / 'events-audit.jsonl.1'
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.failures = 0

    @classmethod
    def protocol(cls, value):
        if value is None:
            return 'absent'
        return value if isinstance(value, str) and value in cls.PROTOCOLS else 'other'

    @staticmethod
    def private(info):
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600

    def write(self, kind, **fields):
        record = {'at': timestamp(time.time()), 'type': kind}
        if kind in ('mcp_request', 'mcp_response'):
            record['method'] = fields.get('method') if fields.get('method') in self.METHODS else 'other'
            if record['method'] == 'tools/call':
                record['tool'] = fields.get('tool') if fields.get('tool') in self.TOOLS else 'other'
            if kind == 'mcp_request':
                record['requested_protocol'] = self.protocol(fields.get('requested_protocol'))
            if kind == 'mcp_response':
                record['outcome'] = 'error' if fields.get('outcome') == 'error' else 'result'
                record['error_code'] = fields.get('error_code') if type(fields.get('error_code')) is int and fields['error_code'] in (-32700, -32600, -32601, -32602, -32603, -32015, -32022) else None
                if record['method'] == 'initialize' and record['outcome'] == 'result':
                    record['negotiated_protocol'] = self.protocol(fields.get('negotiated_protocol'))
        elif kind == 'broker_message_observed':
            record['correlated'] = fields.get('correlated') is True
        elif kind == 'event_request_outcome':
            record.update(method=fields.get('method') if fields.get('method') in EVENT_METHODS else 'other',
                stage=fields.get('stage') if fields.get('stage') in REQUEST_STAGES else 'received',
                outcome='accepted' if fields.get('outcome') == 'accepted' else 'failed',
                code=fields.get('code') if type(fields.get('code')) is int and fields['code'] in (-32600, -32601, -32602, -32603, -32015) else None,
                reason=fields.get('reason') if isinstance(fields.get('reason'), str) and fields['reason'] in REQUEST_REASONS else None)
        else:
            return
        data = (canonical(record) + '\n').encode()
        with self.lock:
            try:
                self.append(data)
            except Exception:
                # Preserve request behavior when diagnostics storage fails.
                # Never retain exception text or recursively log this failure.
                self.failures += 1

    def append(self, data):
        if len(data) > self.max_bytes:
            raise OSError('Audit record exceeds capacity')
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not self.private(info) or info.st_size > self.max_bytes:
                raise OSError('Private bounded audit file required')
            if info.st_size + len(data) > self.max_bytes:
                os.close(fd); fd = None
                if self.previous.exists() or self.previous.is_symlink():
                    if not self.private(self.previous.lstat()):
                        raise OSError('Private audit rotation required')
                os.replace(self.path, self.previous)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('Incomplete audit write')
                view = view[written:]
            os.fsync(fd)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if fd is not None:
                os.close(fd)

    def health(self):
        with self.lock:
            return {'audit_write_failures': self.failures,
                'audit_last_error': 'audit_write_failed' if self.failures else None}


class EventService:
    def __init__(self, engine, keys=None, callback=None, start=True):
        self.engine = engine
        self.keys = keys if keys is not None else Keychain()
        self.callback = callback if callback is not None else HTTPSCallback()
        self.lock = threading.RLock()
        self.request_lock = threading.Lock()
        self.audit = ArrivalAudit(engine.root)
        self.request_diagnostics = {'started_at': timestamp(time.time()), 'methods': {
            method: {'received_count': 0, 'accepted_count': 0, 'failed_count': 0,
                'last_received_at': None, 'last_stage': None, 'last_outcome': None,
                'last_failure': None} for method in EVENT_METHODS}}
        self.stop = threading.Event()
        self.watchers = {}
        self.delivered = set()
        self.pending = queue.Queue(maxsize=128)
        self.verified = {}
        self.diagnostics = {'watch_gaps': 0, 'callback_failures': 0, 'last_error': None}
        self.path = engine.root / 'events.json'
        self.binding = engine.inbox_binding(engine.principal)
        self.policy = engine.inbox_policy()
        self.subscriptions = {}
        self.retired_keys = []
        self.uninitialized = []
        self.revoked = False
        if self.path.exists():
            info = self.path.lstat()
            if not self.path.is_file() or self.path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
                raise ValueError('Owner-only MCP Events metadata required')
            if info.st_size > 1024 * 1024:
                raise ValueError('MCP Events metadata exceeds capacity')
            doc = json.loads(self.path.read_text())
            if doc['binding'] != self.binding:
                raise ValueError('MCP Events metadata belongs to another owner')
            self.retired_keys = doc.get('retired_keys', [])
            if doc['policy'] == self.policy:
                self.subscriptions = doc['subscriptions']
            else:
                for sub in doc['subscriptions'].values():
                    self.retire(sub)
                self.revoked = True
                self.save()
            # A policy change revokes every previous lease before any callback.
            if self.retired_keys:
                self.cleanup_keys()
            if self.subscriptions:
                self.diagnostics['watch_gaps'] += 1
                self.diagnostics['last_error'] = 'facade_restart_no_replay'
        self.started = start
        self.thread = None
        self.delivery_thread = None
        if start:
            self.delivery_thread = threading.Thread(target=self.deliver, daemon=True, name='bridge-mcp-event-delivery')
            self.delivery_thread.start()
            self.thread = threading.Thread(target=self.run, daemon=True, name='bridge-mcp-events')
            self.thread.start()

    def save(self):
        raw = canonical({'binding': self.binding, 'policy': self.policy,
            'subscriptions': self.subscriptions, 'retired_keys': self.retired_keys}).encode()
        if len(raw) > 1024 * 1024:
            raise EventError(-32603, 'subscription_capacity')
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temp.exists():
                temp.unlink()

    def authorize(self):
        if (not self.engine.config.get('events_enabled', False)
                or not self.engine.config.get('inbox_enabled', False)
                or 'bridge:read' not in self.engine.scopes):
            raise EventError(-32601, 'events_disabled')

    def identity(self, params, request=None):
        if params.get('name') != NAME or not isinstance(params.get('arguments', {}), dict):
            raise EventError(-32602, 'invalid_event')
        arguments = params.get('arguments', {})
        if set(arguments) - {'agent_session_id'} or ('agent_session_id' in arguments and not isinstance(arguments['agent_session_id'], str)):
            raise EventError(-32602, 'invalid_arguments')
        delivery = params.get('delivery')
        if not isinstance(delivery, dict) or delivery.get('mode') != 'webhook':
            raise EventError(-32602, 'invalid_delivery')
        url = delivery.get('url')
        try:
            parsed = urlsplit(url)
            if (not isinstance(url, str) or parsed.scheme != 'https' or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment
                    or parsed.port not in (None, 443) or len(url) > 4096
                    or any(ord(char) < 33 or ord(char) == 127 for char in url)):
                raise ValueError()
        except (ValueError, TypeError):
            raise EventError(-32602, 'invalid_callback_url') from None
        self.request_stage(request, 'resolve_routes')
        self.engine.inbox_routes(self.engine.principal, arguments.get('agent_session_id'))
        sid = 'sub_' + hashlib.sha256(canonical([self.binding, NAME, arguments, url]).encode()).hexdigest()
        return sid, arguments, url

    def health(self):
        with self.request_lock:
            requests = copy.deepcopy(self.request_diagnostics)
        with self.lock:
            active = sum(sub['expires'] > time.time() for sub in self.subscriptions.values())
            return dict(self.diagnostics, **self.audit.health(), request_diagnostics=requests, active_subscriptions=active,
                watching_routes=sum(thread.is_alive() and ready.is_set() for thread, ready, _ in self.watchers.values()),
                uninitialized_agent_session_ids=list(self.uninitialized),
                pending_key_deletions=len(self.retired_keys),
                queued_notifications=self.pending.qsize(), replay_supported=False)

    def signed(self, sid, url, secret, value, event_id, previous=None):
        body = canonical(value).encode()
        when = str(int(time.time()))
        signature = base64.b64encode(hmac.digest(signing_key(secret),
            event_id.encode() + b'.' + when.encode() + b'.' + body, 'sha256')).decode()
        signatures = ['v1,' + signature]
        if previous is not None:
            old = base64.b64encode(hmac.digest(signing_key(previous),
                event_id.encode() + b'.' + when.encode() + b'.' + body, 'sha256')).decode()
            signatures.append('v1,' + old)
        headers = {'Content-Type': 'application/json', 'webhook-id': event_id,
            'webhook-timestamp': when, 'webhook-signature': ' '.join(signatures),
            'X-MCP-Subscription-Id': sid}
        return self.callback(url, body, headers)

    def audit_request(self, message):
        if not isinstance(message, dict) or 'method' not in message:
            return
        method = message.get('method')
        method = method if isinstance(method, str) and method in ArrivalAudit.METHODS else 'other'
        params = message.get('params')
        tool = params.get('name') if isinstance(params, dict) else None
        tool = tool if isinstance(tool, str) and tool in ArrivalAudit.TOOLS else 'other'
        meta = params.get('_meta') if isinstance(params, dict) else None
        protocol = (params.get('protocolVersion') if method == 'initialize' and isinstance(params, dict)
                    else meta.get('io.modelcontextprotocol/protocolVersion') if isinstance(meta, dict) else None)
        self.audit.write('mcp_request', method=method, tool=tool,
            requested_protocol=protocol)

    def audit_response(self, message, response):
        if not isinstance(message, dict) or 'method' not in message or not isinstance(response, dict):
            return
        method = message.get('method')
        method = method if isinstance(method, str) and method in ArrivalAudit.METHODS else 'other'
        params = message.get('params')
        tool = params.get('name') if isinstance(params, dict) else None
        tool = tool if isinstance(tool, str) and tool in ArrivalAudit.TOOLS else 'other'
        failure = response.get('error')
        result = response.get('result')
        self.audit.write('mcp_response', method=method, tool=tool,
            outcome='error' if isinstance(failure, dict) else 'result',
            error_code=failure.get('code') if isinstance(failure, dict) else None,
            negotiated_protocol=result.get('protocolVersion') if isinstance(result, dict) else None)

    def observed_message(self, event):
        message = event.get('message')
        self.audit.write('broker_message_observed', correlated=
            isinstance(message, dict) and message.get('reply_to') is not None)

    def request_begin(self, method):
        if method not in EVENT_METHODS:
            return None
        request = {'method': method, 'stage': 'received'}
        with self.request_lock:
            info = self.request_diagnostics['methods'][method]
            info['received_count'] += 1
            info.update(last_received_at=timestamp(time.time()), last_stage='received',
                last_outcome='processing')
        return request

    def request_stage(self, request, stage):
        if request is None:
            return
        if stage not in REQUEST_STAGES:
            raise ValueError('Unknown MCP Events request stage')
        request['stage'] = stage
        with self.request_lock:
            self.request_diagnostics['methods'][request['method']]['last_stage'] = stage

    def request_finish(self, request, failure=None):
        if request is None:
            return
        with self.request_lock:
            info = self.request_diagnostics['methods'][request['method']]
            if failure is None:
                info['accepted_count'] += 1
                info.update(last_stage='complete', last_outcome='accepted')
            else:
                code, reason = failure
                # Do not retain arbitrary exception fields, even from EventError.
                code = code if type(code) is int and code in (-32600, -32601, -32602, -32603, -32015) else -32603
                reason = reason if isinstance(reason, str) and reason in REQUEST_REASONS else 'event_operation_failed'
                info['failed_count'] += 1
                info.update(last_stage=request['stage'], last_outcome='failed',
                    last_failure={'at': timestamp(time.time()), 'code': code,
                        'reason': reason, 'stage': request['stage']})
            audit = {'method': request['method'], 'stage': info['last_stage'],
                'outcome': info['last_outcome'], 'code': None if failure is None else code,
                'reason': None if failure is None else reason}
        self.audit.write('event_request_outcome', **audit)

    def invalid_request(self, method):
        request = self.request_begin(method)
        self.request_stage(request, 'validate')
        self.request_finish(request, (-32600, 'invalid_request'))

    def call(self, method, params):
        request = self.request_begin(method)
        try:
            result = self._call(method, params, request)
        except EventError as error:
            self.request_finish(request, (error.code, error.reason))
            raise
        except Exception as error:
            from mcp_phase1 import Fault
            if isinstance(error, Fault):
                failure = (-32602, 'owner_or_route_unavailable')
            elif request is not None and request['stage'] == 'verify_callback':
                failure = (-32603, 'callback_failed')
            elif request is not None and request['stage'] == 'store_signing_key':
                failure = (-32603, 'keychain_unavailable')
            elif request is not None and request['stage'] in (
                    'reserve_key_reference', 'persist_subscription', 'persist_unsubscribe', 'cleanup_keys'):
                failure = (-32603, 'storage_failed')
            else:
                failure = (-32603, 'event_operation_failed')
            self.request_finish(request, failure)
            raise
        self.request_finish(request)
        return result

    def _call(self, method, params, request):
        self.request_stage(request, 'authorize')
        self.authorize()
        self.request_stage(request, 'validate')
        # MCP RequestParams permits an optional metadata object. Treat it as
        # transport metadata, never as identity, authority or subscription state.
        if '_meta' in params and not isinstance(params['_meta'], dict):
            raise EventError(-32602, 'invalid_arguments')
        if '_meta' in params and 'progressToken' in params['_meta']:
            token = params['_meta']['progressToken']
            if type(token) not in (str, int, float) or (type(token) is float and not math.isfinite(token)):
                raise EventError(-32602, 'invalid_arguments')
        params = {key: value for key, value in params.items() if key != '_meta'}
        if method == 'events/list':
            if params:
                raise EventError(-32602, 'invalid_arguments')
            return {'events': [CATALOG]}
        if method not in ('events/subscribe', 'events/unsubscribe'):
            raise EventError(-32601, 'method_not_found')
        allowed = {'name', 'arguments', 'delivery'}
        if method == 'events/subscribe':
            allowed |= {'cursor', 'ttlMs'}
        if set(params) - allowed:
            raise EventError(-32602, 'invalid_arguments')
        sid, arguments, url = self.identity(params, request)
        with self.lock:
            if method == 'events/unsubscribe':
                self.request_stage(request, 'validate')
                if set(params['delivery']) != {'mode', 'url'}:
                    raise EventError(-32602, 'invalid_delivery')
                old = self.subscriptions.pop(sid, None)
                before = list(self.retired_keys)
                if old is not None:
                    self.retire(old)
                self.request_stage(request, 'persist_unsubscribe')
                try:
                    self.save()
                except Exception:
                    self.retired_keys = before
                    if old is not None:
                        self.subscriptions[sid] = old
                    raise
                self.request_stage(request, 'cleanup_keys')
                self.cleanup_keys()
                return {}
            self.request_stage(request, 'validate')
            delivery = params['delivery']
            if set(delivery) != {'mode', 'url', 'secret'}:
                raise EventError(-32602, 'invalid_delivery')
            secret = delivery['secret']
            signing_key(secret)
            if params.get('cursor') is not None:
                raise EventError(-32602, 'replay_unavailable')
            ttl = params.get('ttlMs', DEFAULT_TTL_MS)
            ttl = DEFAULT_TTL_MS if ttl is None else ttl
            if type(ttl) is not int or ttl <= 0:
                raise EventError(-32602, 'invalid_lifetime')
            if sid not in self.subscriptions and len(self.subscriptions) >= MAX_SUBSCRIPTIONS:
                raise EventError(-32603, 'subscription_capacity')
            verification_key = (url, hashlib.sha256(secret.encode()).hexdigest())
            if self.verified.get(verification_key, 0) <= time.time():
                self.request_stage(request, 'verify_callback')
                challenge = secrets.token_urlsafe(32)
                code, response = self.signed(sid, url, secret,
                    {'type': 'verification', 'challenge': challenge}, 'verify_' + secrets.token_hex(16))
                try:
                    echoed = json.loads(response).get('challenge')
                except (ValueError, AttributeError):
                    echoed = None
                if not 200 <= code < 300 or not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
                    raise EventError(-32015, 'challenge_failed')
                self.verified = {key: until for key, until in self.verified.items() if until > time.time()}
                if len(self.verified) >= MAX_SUBSCRIPTIONS:
                    self.verified.pop(next(iter(self.verified)))
                self.verified[verification_key] = time.time() + 300
            # Watch before granting a lease; no arrival after the accepted
            # subscription is lost to starting the listener asynchronously.
            if self.started:
                self.request_stage(request, 'start_watchers')
                self.ensure_watchers(arguments)
            key_ref = sid + '_' + secrets.token_hex(8)
            # Record a key reference before touching the second durable store.
            # A crash or failed final commit leaves a recoverable cleanup task.
            self.retired_keys.append(key_ref)
            self.request_stage(request, 'reserve_key_reference')
            self.save()
            self.request_stage(request, 'store_signing_key')
            try:
                self.keys.put(key_ref, secret)
            except Exception:
                self.cleanup_keys()
                raise
            expiration = time.time() + min(ttl, MAX_TTL_MS) / 1000
            old = self.subscriptions.get(sid)
            pending_retired_keys = list(self.retired_keys)
            self.subscriptions[sid] = {'name': NAME, 'arguments': arguments, 'url': url,
                'expires': expiration, 'key_ref': key_ref,
                'since': old['since'] if old is not None else time.time()}
            if old is not None:
                self.subscriptions[sid]['previous_key_ref'] = old['key_ref']
                self.subscriptions[sid]['rotation_until'] = time.time() + 60
            self.retired_keys.remove(key_ref)
            if old is not None and old.get('previous_key_ref'):
                self.retired_keys.append(old['previous_key_ref'])
            self.request_stage(request, 'persist_subscription')
            try:
                self.save()
            except Exception:
                # Restore the exact pre-replacement cleanup set. In particular,
                # the restored lease still owns its previous rotation key.
                self.retired_keys = pending_retired_keys
                if old is None:
                    self.subscriptions.pop(sid, None)
                else:
                    self.subscriptions[sid] = old
                # The first metadata commit still holds this pending reference.
                raise
            self.request_stage(request, 'cleanup_keys')
            self.cleanup_keys()
            return {'id': sid, 'refreshBefore': timestamp(expiration), 'cursor': None, 'truncated': False}

    def note(self, reason, field='watch_gaps'):
        with self.lock:
            self.diagnostics[field] += 1
            self.diagnostics['last_error'] = reason

    def retire(self, sub):
        for field in ('key_ref', 'previous_key_ref'):
            if sub.get(field) and sub[field] not in self.retired_keys:
                self.retired_keys.append(sub[field])

    def cleanup_keys(self):
        for reference in list(self.retired_keys):
            try:
                self.keys.delete(reference)
            except Exception:
                self.diagnostics['last_error'] = 'keychain_cleanup_pending'
                continue
            self.retired_keys.remove(reference)
            try:
                self.save()
            except Exception:
                self.retired_keys.append(reference)
                raise

    def ensure_watchers(self, arguments=None):
        selectors = [sub['arguments'].get('agent_session_id') for sub in self.subscriptions.values()
            if sub['expires'] > time.time()]
        if arguments is not None:
            selectors.append(arguments.get('agent_session_id'))
        entries = {}
        for selector in set(selectors):
            for entry in self.engine.inbox_routes(self.engine.principal, selector):
                entries[canonical(entry)] = entry
        actual = {}
        for project in {entry['route']['project'] for entry in entries.values()}:
            actual[project] = self.engine.backend.rpc('routes', project=project)
        desired = {}
        self.uninitialized = []
        for key, entry in entries.items():
            route = entry['route']
            found = next((r for r in actual[route['project']] if r['name'] == route['name']), None)
            if found is None:
                self.uninitialized.append(entry['slot'])
                continue
            if broker.route_key(found) != broker.route_key(route):
                self.note('inbox_route_changed')
                if arguments is not None:
                    raise EventError(-32602, 'inbox_route_changed')
                continue
            desired[key] = entry
        for key, (_, _, stopped) in list(self.watchers.items()):
            if key not in desired:
                stopped.set()
        for key, (thread, _, stopped) in list(self.watchers.items()):
            if stopped.is_set():
                thread.join(timeout=1)
                if not thread.is_alive():
                    del self.watchers[key]
        for key, entry in desired.items():
            if key in self.watchers and self.watchers[key][0].is_alive():
                if not self.watchers[key][1].is_set():
                    raise EventError(-32603, 'inbox_watch_unavailable')
                continue
            ready, stopped = threading.Event(), threading.Event()
            thread = threading.Thread(target=self.watch, args=(entry, ready, stopped), daemon=True)
            self.watchers[key] = (thread, ready, stopped)
            thread.start()
            if not ready.wait(4):
                raise EventError(-32603, 'inbox_watch_unavailable')

    def watch(self, entry, ready, stopped):
        route = entry['route']
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect(str(self.engine.backend.path))
                sock.sendall(broker.wire({'op': 'watch', 'project': route['project'], 'name': route['name']}))
                buffer = b''
                while not self.stop.is_set() and not stopped.is_set():
                    if not select.select([sock], [], [], .25)[0]:
                        continue
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise OSError('watch disconnected')
                    buffer += chunk
                    if len(buffer) > broker.MAX_FRAME:
                        raise OSError('watch frame capacity')
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        event = broker.broker_response(line)
                        if event.get('type') == 'subscribed':
                            ready.set()
                        elif event.get('type') == 'message':
                            self.observed_message(event)
                            try:
                                self.pending.put_nowait((entry, event))
                            except queue.Full:
                                self.note('notification_queue_full')
        except Exception:
            if not self.stop.is_set() and not stopped.is_set():
                self.note('inbox_watch_disconnected')

    def arrival(self, entry, event):
        message = event.get('message', {})
        if event.get('type') != 'message':
            return
        if message.get('reply_to') is not None or broker.route_key(message.get('route', {})) != broker.route_key(entry['route']):
            return
        with self.lock:
            self.authorize()
            for sid, sub in list(self.subscriptions.items()):
                if self.stop.is_set():
                    return
                if sub['expires'] <= time.time() or datetime.fromisoformat(message['created']).timestamp() < sub['since']:
                    continue
                selector = sub['arguments'].get('agent_session_id')
                if selector is not None and selector != entry['slot']:
                    continue
                try:
                    routes = self.engine.inbox_routes(self.engine.principal, selector)
                    if entry not in routes:
                        continue
                    # Broker validates the exact route atomically; never trust
                    # the stale watch registration after route replacement.
                    self.engine.inbox_rpc('inbox_get', self.engine.principal, [entry],
                        event['instance'], id=message['id'], offset=0, max_chars=1)
                    event_id = 'evt_' + hashlib.sha256(canonical([sid, event['instance'], message['id']]).encode()).hexdigest()
                    if event_id in self.delivered:
                        continue
                    if len(self.delivered) >= 4096:
                        self.delivered.pop()
                    # Consume the volatile attempt before I/O, never on ACK.
                    self.delivered.add(event_id)
                    value = {'eventId': event_id, 'name': NAME,
                        'timestamp': message['created'], 'data': {'agent_session_id': entry['slot']}, 'cursor': None}
                    previous = self.keys.get(sub['previous_key_ref']) if sub.get('rotation_until', 0) > time.time() else None
                    secret = self.keys.get(sub['key_ref'])
                    # Keychain may block. Recheck lease and stop after it returns,
                    # immediately before initiating the external request.
                    if self.stop.is_set() or sub['expires'] <= time.time():
                        continue
                    self.authorize()
                    code, _ = self.signed(sid, sub['url'], secret, value, event_id, previous)
                    if not 200 <= code < 300:
                        raise EventError(-32015, 'callback_rejected')
                except Exception:
                    # A request may have reached ChatGPT. Never retry this
                    # ambiguous callback, and never acknowledge the inbox.
                    self.note('event_callback_failed', 'callback_failures')

    def deliver(self):
        while not self.stop.is_set():
            try:
                entry, event = self.pending.get(timeout=.25)
            except queue.Empty:
                continue
            self.arrival(entry, event)

    def run(self):
        while not self.stop.wait(.5):
            try:
                with self.lock:
                    before_subscriptions = {sid: dict(sub) for sid, sub in self.subscriptions.items()}
                    before_keys = list(self.retired_keys)
                    expired = [sid for sid, sub in self.subscriptions.items() if sub['expires'] <= time.time()]
                    for sid in expired:
                        self.retire(self.subscriptions.pop(sid))
                    for sub in self.subscriptions.values():
                        if sub.get('previous_key_ref') and sub['rotation_until'] <= time.time():
                            self.retired_keys.append(sub.pop('previous_key_ref'))
                            sub.pop('rotation_until')
                    if before_subscriptions != self.subscriptions or before_keys != self.retired_keys:
                        try:
                            self.save()
                        except Exception:
                            self.subscriptions = before_subscriptions
                            self.retired_keys = before_keys
                            raise
                    self.cleanup_keys()
                    self.ensure_watchers()
            except Exception:
                self.note('inbox_watch_unavailable')

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)
        if self.delivery_thread:
            self.delivery_thread.join(timeout=15)
        for thread, _, stopped in self.watchers.values():
            stopped.set()
            thread.join(timeout=15)
        threads = [self.thread, self.delivery_thread] + [item[0] for item in self.watchers.values()]
        if any(thread is not None and thread.is_alive() for thread in threads):
            raise RuntimeError('MCP Events worker still active; owner ledger remains locked')
