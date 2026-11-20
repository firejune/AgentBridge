"""Deliver standalone peer tool output to an existing Codex desktop owner.

Desktop IPC is an internal, versioned protocol. Never create a router/server,
resume a thread, send human input, or retry an ambiguous mutation here.
"""
import errno
import json
import os
from pathlib import Path
import socket
import stat
import struct
from subprocess import TimeoutExpired
import time
import uuid

MAX_FRAME = 1024 * 1024
# Both versions retain the exact-owner snapshot envelope and permission fields.
# Version 13 was measured against Desktop on 2026-11-15; unknown versions fail closed.
SNAPSHOT_VERSIONS = (11, 13)


class DeliveryError(ValueError):
    def __init__(self, reason, uncertain=False):
        super().__init__(reason)
        self.uncertain = uncertain


class ThreadArchived(DeliveryError):
    MESSAGE = 'Codex thread is archived. Message was not delivered.'

    def __init__(self):
        super().__init__(self.MESSAGE)


class ResponseTimeout(DeliveryError):
    """The bounded IPC response deadline expired."""


class EndpointUnavailable(DeliveryError):
    DEFAULT = '~/.codex/ipc/ipc.sock'
    CONFIGURED = 'configured IPC endpoint'
    MISSING = 'is missing'
    REFUSED = 'has no listening server'
    UNSAFE = 'is inaccessible or is not an owned private socket'
    TIMEOUT = 'timed out during connection'
    UNAVAILABLE = 'could not be connected'
    INITIALIZE = 'failed the initialization handshake'

    def __init__(self, endpoint_name, reason):
        super().__init__(f'Codex Desktop IPC ({endpoint_name}) {reason}. Message was not delivered.')

    @classmethod
    def details(cls):
        return tuple(str(cls(endpoint_name, reason)) for endpoint_name in (cls.DEFAULT, cls.CONFIGURED)
                     for reason in (cls.MISSING, cls.REFUSED, cls.UNSAFE, cls.TIMEOUT,
                                    cls.UNAVAILABLE, cls.INITIALIZE))


class OwnerUnavailable(DeliveryError):
    TIMEOUT = 'Codex Desktop owner discovery timed out for the exact thread. Message was not delivered.'
    UNAVAILABLE = 'Codex Desktop owner discovery was unavailable for the exact thread. Message was not delivered.'
    NO_OWNER = 'Exact Codex thread has no owner supporting untrusted input. Message was not delivered.'
    WORKSPACE_MISSING = ' The exact saved workspace is missing.'

    @classmethod
    def details(cls):
        return tuple(detail + suffix for detail in (cls.TIMEOUT, cls.UNAVAILABLE, cls.NO_OWNER)
                     for suffix in ('', cls.WORKSPACE_MISSING))


def endpoint():
    override = os.environ.get('AGENT_BRIDGE_CODEX_IPC')
    base = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
    return Path(override).expanduser() if override else base / 'ipc' / 'ipc.sock'


def validate_endpoint(path, endpoint_name=EndpointUnavailable.CONFIGURED):
    """Only connect to an existing socket in the current user's private directory."""
    info, parent = path.lstat(), path.parent.lstat()
    uid = os.getuid()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid
            or parent.st_mode & 0o022):
        raise EndpointUnavailable(endpoint_name, EndpointUnavailable.UNSAFE)


class Client:
    def __init__(self, path=None, timeout=10):
        self.endpoint_name = (EndpointUnavailable.CONFIGURED
                              if path is not None or os.getenv('AGENT_BRIDGE_CODEX_IPC') or os.getenv('CODEX_HOME')
                              else EndpointUnavailable.DEFAULT)
        try:
            self.path = Path(path) if path is not None else endpoint()
        except (RuntimeError, ValueError, TypeError):
            raise EndpointUnavailable(self.endpoint_name, EndpointUnavailable.UNAVAILABLE) from None
        self.timeout = timeout
        self.sock = None
        self.client_id = 'initializing-client'
        self.mutation_sent = False

    def connect(self):
        try:
            validate_endpoint(self.path, self.endpoint_name)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(self.path))
        except TimeoutError:
            raise EndpointUnavailable(self.endpoint_name, EndpointUnavailable.TIMEOUT) from None
        except OSError as error:
            reason = {
                errno.ENOENT: EndpointUnavailable.MISSING,
                errno.ECONNREFUSED: EndpointUnavailable.REFUSED,
                errno.EACCES: EndpointUnavailable.UNSAFE,
                errno.EPERM: EndpointUnavailable.UNSAFE,
                errno.ENOTDIR: EndpointUnavailable.UNSAFE,
                errno.ETIMEDOUT: EndpointUnavailable.TIMEOUT,
            }.get(error.errno, EndpointUnavailable.UNAVAILABLE)
            raise EndpointUnavailable(self.endpoint_name, reason) from None
        except EndpointUnavailable:
            raise
        except ValueError:
            raise EndpointUnavailable(self.endpoint_name, EndpointUnavailable.UNAVAILABLE) from None

    def __enter__(self):
        try:
            self.connect()
            try:
                response = self.call('initialize', {'clientType': 'agent-bridge'}, 0)
            except (OSError, DeliveryError):
                raise EndpointUnavailable(self.endpoint_name, EndpointUnavailable.INITIALIZE) from None
            result = response.get('result')
            client_id = result.get('clientId') if isinstance(result, dict) else None
            if response.get('resultType') != 'success' or not isinstance(client_id, str) or not client_id:
                raise EndpointUnavailable(self.endpoint_name, EndpointUnavailable.INITIALIZE)
            self.client_id = client_id
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __exit__(self, *_):
        self.close()

    def write(self, value, mutation=False):
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        if not 0 < len(raw) <= MAX_FRAME:
            raise DeliveryError('Codex desktop IPC frame exceeds the adapter limit')
        if mutation:
            # sendall may fail after a partial write. Conservatively block replay.
            self.mutation_sent = True
        self.sock.sendall(struct.pack('<I', len(raw)) + raw)

    def exact(self, count, deadline):
        parts = bytearray()
        while len(parts) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResponseTimeout('Codex desktop IPC response timed out')
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(count - len(parts))
            if not chunk:
                raise DeliveryError('Codex desktop IPC closed before its response')
            parts.extend(chunk)
        return parts

    def call(self, method, params, version, owner=None, mutation=False):
        request_id = str(uuid.uuid4())
        request = dict(type='request', requestId=request_id, sourceClientId=self.client_id,
                       version=version, method=method, params=params,
                       timeoutMs=max(1, int(self.timeout * 1000)))
        if owner is not None:
            request['targetClientId'] = owner
        self.write(request, mutation)
        deadline = time.monotonic() + self.timeout
        while True:
            size = struct.unpack('<I', self.exact(4, deadline))[0]
            if not 0 < size <= MAX_FRAME:
                raise DeliveryError('Codex desktop IPC returned an invalid frame size')
            raw = self.exact(size, deadline)
            try:
                response = json.loads(raw)
            except (ValueError, UnicodeError):
                raise DeliveryError('Codex desktop IPC returned invalid JSON') from None
            if not isinstance(response, dict):
                raise DeliveryError('Codex desktop IPC returned an invalid response')
            if response.get('type') == 'client-discovery-request':
                self.write(dict(type='client-discovery-response',
                                requestId=response.get('requestId'), response={'canHandle': False}))
            if response.get('type') != 'response' or response.get('requestId') != request_id:
                continue
            if response.get('resultType') not in ('success', 'error'):
                raise DeliveryError('Codex desktop IPC returned no result type')
            if response['resultType'] == 'success':
                if response.get('method') != method or (owner is not None
                        and response.get('handledByClientId') != owner):
                    raise DeliveryError('Codex desktop IPC returned a different method or owner')
            return response

    def snapshot(self, thread, owner):
        """Read one exact owner's versioned snapshot; never claim ownership."""
        thread = str(uuid.UUID(thread))
        def following(value):
            self.write(dict(type='broadcast', method='thread-stream-following-changed',
                sourceClientId=self.client_id, version=1, targetClientIds=[owner],
                params=dict(hostId='local', conversationId=thread, following=value)))
        complete = False
        try:
            following(True)
            deadline = time.monotonic() + self.timeout
            while True:
                size = struct.unpack('<I', self.exact(4, deadline))[0]
                if not 0 < size <= MAX_FRAME:
                    raise DeliveryError('Codex desktop snapshot exceeds the adapter limit')
                try:
                    response = json.loads(self.exact(size, deadline))
                except (ValueError, UnicodeError):
                    raise DeliveryError('Codex desktop snapshot returned invalid JSON') from None
                if not isinstance(response, dict):
                    raise DeliveryError('Codex desktop snapshot returned an invalid frame')
                if response.get('type') == 'client-discovery-request':
                    self.write(dict(type='client-discovery-response',
                        requestId=response.get('requestId'), response={'canHandle':False}))
                    continue
                if response.get('type') != 'broadcast' or response.get('method') != 'thread-stream-state-changed':
                    continue
                params = response.get('params')
                if not isinstance(params, dict) or params.get('conversationId') != thread:
                    continue
                if (response.get('sourceClientId') != owner or response.get('version') not in SNAPSHOT_VERSIONS
                        or params.get('hostId') != 'local'
                        or not isinstance(response.get('targetClientIds'), list)
                        or self.client_id not in response['targetClientIds']):
                    raise DeliveryError('Codex desktop snapshot owner, version, or destination differs')
                change = params.get('change')
                if not isinstance(change, dict) or change.get('type') != 'snapshot':
                    continue  # Patches are not a complete permission reading.
                state = change.get('conversationState')
                if not isinstance(state, dict) or state.get('id') != thread:
                    raise DeliveryError('Codex desktop snapshot returned a different thread')
                complete = True
                return state
        except OSError:
            raise DeliveryError('Codex desktop snapshot transport failed or timed out') from None
        finally:
            try:
                following(False)
            except (OSError, DeliveryError):
                # A secondary cleanup error must not replace the primary error.
                if complete:
                    raise DeliveryError('Codex desktop snapshot unsubscribe failed') from None

    def owner(self, thread):
        try:
            response = self.call('thread-owner-discovery',
                                 {'hostId': 'local', 'conversationId': thread}, 1)
        except (ResponseTimeout, TimeoutError):
            raise OwnerUnavailable(OwnerUnavailable.TIMEOUT) from None
        except (DeliveryError, OSError):
            raise OwnerUnavailable(OwnerUnavailable.UNAVAILABLE) from None
        if response['resultType'] == 'error':
            if response.get('method', 'thread-owner-discovery') != 'thread-owner-discovery':
                raise OwnerUnavailable(OwnerUnavailable.UNAVAILABLE)
            if response.get('error') == 'no-client-found' and response.get('handledByClientId') is None:
                raise OwnerUnavailable(OwnerUnavailable.NO_OWNER)
            raise OwnerUnavailable(OwnerUnavailable.UNAVAILABLE)
        result = response.get('result')
        owner = response.get('handledByClientId')
        if not isinstance(owner, str) or not owner or not isinstance(result, dict):
            raise OwnerUnavailable(OwnerUnavailable.UNAVAILABLE)
        if result.get('supportsUntrustedAppInput') is not True:
            raise OwnerUnavailable(OwnerUnavailable.NO_OWNER)
        return owner


def payloads(route, message_id, text):
    """No outstanding tool call ID and no nonempty human user input."""
    thread = str(uuid.UUID(route['session']))
    tool = {'name': 'agent_bridge', 'namespace': None, 'output': text}
    restore = dict(id=message_id, text=text, createdAt=int(time.time() * 1000),
                   cwd=route['cwd'], context=dict(prompt=text, addedFiles=[], fileAttachments=[],
                   ideContext=None, imageAttachments=[], workspaceRoots=[route['cwd']]))
    steer = dict(conversationId=thread, clientUserMessageId=message_id, input=[],
                 attachments=[], toolOutput=tool, restoreMessage=restore)
    start = dict(conversationId=thread, turnStart=dict(
        request=dict(threadId=thread, clientUserMessageId=message_id, input=[], toolOutput=tool),
        context=dict(inheritThreadSettings=True, writingBlockContextPrepared=True)))
    return thread, steer, start


def accepted_turn(response):
    """Follower result wraps the owner's steer or turn-start result once."""
    result = response.get('result')
    result = result.get('result') if isinstance(result, dict) else None
    if not isinstance(result, dict):
        raise DeliveryError('Codex desktop returned no accepted turn result', uncertain=True)
    turn = result.get('turn')
    turn_id = result.get('turnId') or (turn.get('id') if isinstance(turn, dict) else None)
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise DeliveryError('Codex desktop returned no accepted turn ID', uncertain=True)
    return turn_id


def exact_saved_workspace(route):
    import codex_preview

    try:
        cwd, archived = codex_preview.saved_thread(route)
        saved, registered = Path(cwd), Path(route['cwd'])
        if saved.is_absolute() and registered.is_absolute() and saved.resolve() == registered.resolve():
            return saved, archived
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, TimeoutExpired):
        # Metadata lookup can be unavailable. Keep the existing IPC outcome
        # semantics unless the exact saved thread was confirmed archived.
        pass
    return None, None


def deliver(route, message_id, text, timeout=10, dry_run=False, path=None):
    workspace, archived = exact_saved_workspace(route)
    if archived is True:
        raise ThreadArchived()
    try:
        thread, steer, start = payloads(route, message_id, text)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DeliveryError('Codex desktop delivery payload is invalid') from None
    client = Client(path, timeout)
    try:
        with client:
            try:
                owner = client.owner(thread)
            except OwnerUnavailable as error:
                if workspace is not None and archived is False:
                    try:
                        workspace.stat()
                    except FileNotFoundError:
                        raise OwnerUnavailable(str(error) + OwnerUnavailable.WORKSPACE_MISSING) from None
                    except OSError:
                        pass
                raise
            if dry_run:
                return dict(sent=False, transport='codex', session=thread, owner=owner,
                            input_role='toolOutput', methods=['thread-follower-steer-turn',
                            'thread-follower-start-turn'])
            response = client.call('thread-follower-steer-turn', steer, 1, owner, mutation=True)
            mode = 'active'
            if response['resultType'] == 'error':
                # Installed owner errors omit method/owner. The pinned target,
                # same connection and strict requestId correlate this negative
                # reply; reject identity fields if an implementation includes
                # them and they disagree. Only this pre-submission error permits
                # one idle call, never a retry of an ambiguous mutation.
                inactive = f'Cannot steer conversation {thread} because its active turn already ended'
                if (response.get('error') != inactive
                        or ('method' in response and response['method'] != 'thread-follower-steer-turn')
                        or ('handledByClientId' in response and response['handledByClientId'] != owner)):
                    raise DeliveryError('Codex desktop active submission returned an unconfirmed error', True)
                client.mutation_sent = False
                response = client.call('thread-follower-start-turn', start, 2, owner, mutation=True)
                mode = 'idle'
            if response['resultType'] != 'success':
                raise DeliveryError('Codex desktop idle submission returned an unconfirmed error', True)
            return dict(session=thread, owner=owner, turn_id=accepted_turn(response),
                        delivery_mode=mode, input_role='toolOutput')
    except DeliveryError as error:
        if client.mutation_sent:
            error.uncertain = True
        raise
    except (OSError, ValueError, TypeError, KeyError):
        # Never include arbitrary owner errors or peer content in logs.
        raise DeliveryError('Codex desktop delivery transport or response failed',
                            client.mutation_sent) from None
