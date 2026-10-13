import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import codex_live


class FakeDesktop:
    """An isolated versioned IPC owner; never uses a model or real app socket."""
    def __init__(self, behavior='active', fragmented=False):
        self.behavior, self.fragmented = behavior, fragmented
        self.owner = str(uuid.uuid4())
        self.calls = []
        self.stop = threading.Event()
        self.errors = []

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-ipc-', dir='/tmp')
        self.path = str(Path(self.temp.name) / 'ipc.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen()
        self.server.settimeout(.05)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.server.close()
        self.thread.join(2)
        self.temp.cleanup()
        if self.errors:
            raise AssertionError(self.errors)

    def exact(self, sock, count):
        raw = bytearray()
        while len(raw) < count:
            chunk = sock.recv(count - len(raw))
            if not chunk:
                return None
            raw.extend(chunk)
        return raw

    def write(self, sock, value):
        raw = json.dumps(value).encode()
        frame = struct.pack('<I', len(raw)) + raw
        if self.fragmented:
            for start in range(0, len(frame), 3):
                sock.sendall(frame[start:start + 3])
        else:
            sock.sendall(frame)

    def handle(self, sock, request):
        self.calls.append(request)
        method = request['method']
        result = dict(type='response', requestId=request['requestId'], resultType='success',
                      method=method, handledByClientId=self.owner)
        if method == 'initialize':
            result['result'] = {'clientId': str(uuid.uuid4())}
        elif method == 'thread-owner-discovery':
            if self.behavior == 'no-owner':
                result.update(resultType='error', error='no-client-found')
            else:
                result['result'] = {'supportsUntrustedAppInput': self.behavior != 'no-capability'}
        elif method == 'thread-follower-steer-turn':
            if self.behavior in ('idle', 'idle-disconnect', 'wrong-owner-inactive',
                                 'wrong-method-inactive'):
                thread = request['params']['conversationId']
                result.update(resultType='error', error=f'Cannot steer conversation {thread} because its active turn already ended')
                if self.behavior == 'wrong-owner-inactive':
                    result['handledByClientId'] = 'different-owner'
                elif self.behavior == 'wrong-method-inactive':
                    result['method'] = 'different-method'
                else:
                    # The installed owner catch emits no identity fields; the
                    # client correlates the response by its pinned request ID.
                    result.pop('handledByClientId')
                    result.pop('method')
            elif self.behavior == 'unknown-error':
                result.update(resultType='error', error='owner echoed SECRET_PEER_BODY')
            elif self.behavior == 'different-inactive':
                result.update(resultType='error', error='Cannot steer another conversation because its active turn already ended')
            elif self.behavior == 'disconnect':
                return False
            elif self.behavior == 'timeout':
                self.stop.wait(.3)
                return False
            elif self.behavior == 'malformed':
                sock.sendall(struct.pack('<I', 3) + b'bad')
                return False
            elif self.behavior == 'oversized':
                sock.sendall(struct.pack('<I', codex_live.MAX_FRAME + 1))
                return False
            elif self.behavior == 'wrong-owner':
                result['handledByClientId'] = 'different-owner'
                result['result'] = {'result': {'turnId': 'active-turn'}}
            elif self.behavior == 'no-turn-id':
                result['result'] = {'result': {}}
            else:
                # Discovery from other clients must never make us an owner.
                self.write(sock, {'type': 'client-discovery-request', 'requestId': 'discovery'})
                result['result'] = {'result': {'turnId': 'active-turn'}}
        elif method == 'thread-follower-start-turn':
            if self.behavior == 'idle-disconnect':
                return False
            result['result'] = {'result': {'turn': {'id': 'idle-turn'}}}
        elif request['type'] == 'client-discovery-response':
            return True
        else:
            raise AssertionError(method)
        self.write(sock, result)
        return True

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    sock, _ = self.server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with sock:
                    sock.settimeout(1)
                    while not self.stop.is_set():
                        header = self.exact(sock, 4)
                        if header is None:
                            break
                        raw = self.exact(sock, struct.unpack('<I', header)[0])
                        if raw is None:
                            break
                        request = json.loads(raw)
                        if request.get('type') == 'client-discovery-response':
                            self.calls.append(request)
                            continue
                        if not self.handle(sock, request):
                            break
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.errors.append(repr(error))


class CodexLiveTests(unittest.TestCase):
    def setUp(self):
        self.route = {'session': str(uuid.uuid4()), 'cwd': '/tmp'}
        self.message_id = str(uuid.uuid4())

    def deliver(self, desktop, **kwargs):
        return codex_live.deliver(self.route, self.message_id, 'PEER_BODY',
                                  path=desktop.path, timeout=.15, **kwargs)

    def test_active_owner_receives_standalone_output_with_split_frames(self):
        with FakeDesktop(fragmented=True) as desktop:
            result = self.deliver(desktop)
            self.assertEqual(result['turn_id'], 'active-turn')
            self.assertEqual(result['delivery_mode'], 'active')
            requests = [c for c in desktop.calls if c.get('method')]
            self.assertEqual([c['method'] for c in requests], ['initialize', 'thread-owner-discovery', 'thread-follower-steer-turn'])
            mutation = requests[-1]
            self.assertEqual(mutation['version'], 1)
            self.assertEqual(mutation['targetClientId'], desktop.owner)
            self.assertNotIn('hostId', mutation)
            params = mutation['params']
            self.assertEqual(params['conversationId'], self.route['session'])
            self.assertEqual(params['input'], [])
            self.assertEqual(params['toolOutput'], {'name': 'agent_bridge', 'namespace': None, 'output': 'PEER_BODY'})
            self.assertNotIn('call_id', json.dumps(params))
            self.assertNotIn('callId', json.dumps(params))

    def test_exact_inactive_result_starts_same_owner_with_inherited_settings(self):
        with FakeDesktop('idle') as desktop:
            result = self.deliver(desktop)
            self.assertEqual(result['turn_id'], 'idle-turn')
            start = desktop.calls[-1]
            self.assertEqual(start['version'], 2)
            self.assertEqual(start['targetClientId'], desktop.owner)
            request = start['params']['turnStart']['request']
            self.assertEqual(request['threadId'], self.route['session'])
            self.assertEqual(request['clientUserMessageId'], self.message_id)
            self.assertEqual(request['input'], [])
            self.assertEqual(set(request), {'threadId', 'clientUserMessageId', 'input', 'toolOutput'})
            self.assertTrue(start['params']['turnStart']['context']['inheritThreadSettings'])

    def test_preflight_failure_never_mutates(self):
        for behavior in ('no-owner', 'no-capability'):
            with self.subTest(behavior=behavior), FakeDesktop(behavior) as desktop:
                with self.assertRaises(codex_live.DeliveryError) as raised:
                    self.deliver(desktop)
                self.assertFalse(raised.exception.uncertain)
                self.assertEqual(len(desktop.calls), 2)

    def test_unknown_postwrite_results_are_uncertain_without_fallback(self):
        for behavior in ('unknown-error', 'different-inactive', 'disconnect', 'timeout',
                         'malformed', 'oversized', 'wrong-owner', 'no-turn-id',
                         'wrong-owner-inactive', 'wrong-method-inactive'):
            with self.subTest(behavior=behavior), FakeDesktop(behavior) as desktop:
                with self.assertRaises(codex_live.DeliveryError) as raised:
                    self.deliver(desktop)
                self.assertTrue(raised.exception.uncertain)
                self.assertNotIn('SECRET_PEER_BODY', str(raised.exception))
                self.assertEqual(len(desktop.calls), 3)

    def test_idle_disconnect_does_not_retry(self):
        with FakeDesktop('idle-disconnect') as desktop:
            with self.assertRaises(codex_live.DeliveryError) as raised:
                self.deliver(desktop)
            self.assertTrue(raised.exception.uncertain)
            self.assertEqual(len(desktop.calls), 4)

    def test_dry_run_only_discovers_owner(self):
        with FakeDesktop() as desktop:
            result = self.deliver(desktop, dry_run=True)
            self.assertFalse(result['sent'])
            self.assertEqual(len(desktop.calls), 2)

    def test_missing_and_unowned_socket_fail_before_submission(self):
        with tempfile.TemporaryDirectory(prefix='ab-noipc-', dir='/tmp') as temp:
            with self.assertRaises(codex_live.DeliveryError) as raised:
                codex_live.deliver(self.route, self.message_id, 'PEER', path=Path(temp) / 'absent')
            self.assertFalse(raised.exception.uncertain)
        with FakeDesktop() as desktop:
            with patch.object(codex_live.os, 'getuid', return_value=os.getuid() + 1):
                with self.assertRaises(codex_live.DeliveryError) as raised:
                    self.deliver(desktop)
            self.assertFalse(raised.exception.uncertain)

    def test_cancel_after_write_leaves_no_replay_path(self):
        with FakeDesktop() as desktop:
            original = codex_live.Client.call
            def cancelled(client, method, *args, **kwargs):
                if method == 'thread-follower-steer-turn':
                    client.mutation_sent = True
                    raise KeyboardInterrupt
                return original(client, method, *args, **kwargs)
            with patch.object(codex_live.Client, 'call', cancelled):
                with self.assertRaises(KeyboardInterrupt):
                    self.deliver(desktop)


if __name__ == '__main__':
    unittest.main()
