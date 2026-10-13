"""Deliver standalone peer tool output to an existing Codex desktop owner.

Desktop IPC is an internal, versioned protocol. Never create a router/server,
resume a thread, send human input, or retry an ambiguous mutation here.
"""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
import uuid

MAX_FRAME = 1024 * 1024


class DeliveryError(ValueError):
    def __init__(self, reason, uncertain=False):
        super().__init__(reason)
        self.uncertain = uncertain


def endpoint():
    override = os.environ.get('AGENT_BRIDGE_CODEX_IPC')
    base = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
    return Path(override).expanduser() if override else base / 'ipc' / 'ipc.sock'


def validate_endpoint(path):
    """Only connect to an existing socket in the current user's private directory."""
    info, parent = path.lstat(), path.parent.lstat()
    uid = os.getuid()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid
            or parent.st_mode & 0o022):
        raise DeliveryError('Codex desktop IPC is not an owned private socket')


class Client:
    def __init__(self, path=None, timeout=10):
        self.path = Path(path) if path is not None else endpoint()
        self.timeout = timeout
        self.sock = None
        self.client_id = 'initializing-client'
        self.mutation_sent = False

    def __enter__(self):
        try:
            validate_endpoint(self.path)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(self.path))
            response = self.call('initialize', {'clientType': 'agent-bridge'}, 0)
            result = response.get('result')
            if response.get('resultType') != 'success' or not isinstance(result, dict):
                raise DeliveryError('Codex desktop IPC initialization rejected')
            client_id = result.get('clientId')
            if not isinstance(client_id, str) or not client_id:
                raise DeliveryError('Codex desktop IPC returned no client ID')
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
                raise DeliveryError('Codex desktop IPC response timed out')
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
            try:
                response = json.loads(self.exact(size, deadline))
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

    def owner(self, thread):
        response = self.call('thread-owner-discovery',
                             {'hostId': 'local', 'conversationId': thread}, 1)
        result = response.get('result')
        owner = response.get('handledByClientId')
        if (response['resultType'] != 'success' or not isinstance(owner, str) or not owner
                or not isinstance(result, dict) or result.get('supportsUntrustedAppInput') is not True):
            raise DeliveryError('Exact Codex thread has no owner supporting untrusted input')
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


def deliver(route, message_id, text, timeout=10, dry_run=False, path=None):
    client = Client(path, timeout)
    try:
        thread, steer, start = payloads(route, message_id, text)
        with client:
            owner = client.owner(thread)
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
        raise DeliveryError('Codex desktop delivery transport or payload failed',
                            client.mutation_sent) from None
