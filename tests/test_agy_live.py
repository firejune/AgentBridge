import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
from support import RunningBroker

CSRF = 'csrf-' + 'ab' * 16


class FakeAgyServer:
    """Isolated local HTTP ConnectRPC stub for Antigravity language_server."""

    def __init__(self, root, sid, other_sid=None, send_behavior='ok'):
        self.root = Path(root).resolve()
        self.sid = sid
        self.other_sid = other_sid or str(uuid.uuid4())
        self.send_behavior = send_behavior
        self.requests = []
        self.trajectories = {
            self.sid: {
                'summary': 'Agent-Bridge Skill Verification',
                'status': 'CASCADE_RUN_STATUS_RUNNING',
                'workspaces': [{'workspaceFolderAbsoluteUri': self.root.as_uri()}],
                'lastModifiedTime': '2026-10-14T12:00:00Z',
            },
            self.other_sid: {
                'summary': 'Other Workspace Session',
                'status': 'CASCADE_RUN_STATUS_IDLE',
                'workspaces': [{'workspaceFolderAbsoluteUri': 'file:///another/workspace'}],
                'lastModifiedTime': '2026-10-14T11:00:00Z',
            },
        }

    def __enter__(self):
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                token = self.headers.get('x-codeium-csrf-token')
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
                parent.requests.append((self.path, token, body))
                if token != CSRF:
                    self.send_response(403)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'code': 'permission_denied', 'message': 'invalid csrf'}).encode())
                    return
                if self.path.endswith('/GetAllCascadeTrajectories'):
                    self.respond(200, {'trajectorySummaries': parent.trajectories})
                elif self.path.endswith('/GetConversationMetadata'):
                    cid = body.get('conversationId')
                    traj = parent.trajectories.get(cid)
                    if not traj:
                        self.respond(200, {'code': 'unknown', 'message': f'trajectory not found: {cid}'})
                    else:
                        self.respond(200, {'metadata': {'workspaces': traj['workspaces']}})
                elif self.path.endswith('/SendAgentMessage'):
                    if parent.send_behavior == 'error':
                        self.respond(500, {'code': 'internal', 'message': 'message hub failure'})
                    elif parent.send_behavior == 'invalid-json':
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'not-json')
                    else:
                        self.respond(200, {})
                elif self.path.endswith('/SendUserCascadeMessage'):
                    cid = body.get('cascadeId')
                    traj = parent.trajectories.get(cid)
                    if not traj:
                        self.respond(500, {'code': 'unknown', 'message': f'trajectory not found: {cid}'})
                    elif traj.get('status') == 'CASCADE_RUN_STATUS_RUNNING':
                        if parent.send_behavior == 'finish-after-first-wake':
                            traj['status'] = 'CASCADE_RUN_STATUS_IDLE'
                        self.respond(500, {'code': 'internal', 'message': 'cannot queue empty user input step'})
                    else:
                        traj['status'] = 'CASCADE_RUN_STATUS_RUNNING'
                        self.respond(200, {})
                else:
                    self.respond(404, {'code': 'unimplemented', 'message': self.path})

            def respond(self, code, payload):
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode('utf-8'))

            def log_message(self, *_):
                pass

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_port}'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)


class AgyLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-agy-', dir='/tmp')
        self.root = Path(self.temp.name).resolve()
        self.sid = str(uuid.uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def env(self, fake):
        return {
            **os.environ,
            'AGENT_BRIDGE_AGY_URL': fake.url,
            'AGENT_BRIDGE_AGY_CSRF': CSRF,
        }

    def cli(self, server_path, env, *args, stdin='Question for Jinni'):
        return subprocess.run(
            [sys.executable, bridge.__file__, '--socket', str(server_path), *args],
            input=stdin, capture_output=True, text=True, env=env,
        )

    def test_sessions_and_session_discovery_filter_and_verify_exact_uuid(self):
        with FakeAgyServer(self.root, self.sid) as fake:
            env = self.env(fake)
            sock = self.root / 'unused.sock'
            res = self.cli(sock, env, 'sessions', '--provider', 'agy', '--cwd', str(self.root), '--search', 'skill')
            self.assertEqual(res.returncode, 0, res.stdout)
            rows = json.loads(res.stdout)['providers']['agy']['sessions']
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['id'], rows[0]['status'], rows[0]['cwd']),
                             (self.sid, 'running', str(self.root)))

            single = self.cli(sock, env, 'session', 'agy', self.sid)
            self.assertEqual(single.returncode, 0, single.stdout)
            self.assertEqual(json.loads(single.stdout)['session']['id'], self.sid)

            missing = self.cli(sock, env, 'session', 'agy', str(uuid.uuid4()))
            self.assertEqual(missing.returncode, 1)
            self.assertIn('trajectory not found', json.loads(missing.stdout)['error'])

    def test_send_pushes_immediately_to_live_agy_session_and_deduplicates(self):
        with FakeAgyServer(self.root, self.sid) as fake, RunningBroker() as server:
            env = self.env(fake)
            reg = self.cli(server.path, env, 'register', 'jinni', '--project', 'demo',
                           '--transport', 'agy', '--session', self.sid, '--discover')
            self.assertEqual(reg.returncode, 0, reg.stdout)
            self.assertEqual(json.loads(reg.stdout)['cwd'], str(self.root))
            server.call('register', project='demo', name='naru', cwd=str(self.root))

            sent = self.cli(server.path, env, 'send', '--project', 'demo',
                            '--from', 'naru', '--to', 'jinni', '--key', 'k1', '--file', '-')
            self.assertEqual(sent.returncode, 0, sent.stdout)
            msg = json.loads(sent.stdout)
            self.assertEqual(msg['state'], 'submitted')
            self.assertEqual(msg['events'][-1]['event'], 'pushed')
            self.assertNotIn(CSRF, sent.stdout + sent.stderr)

            send_calls = [body for path, _, body in fake.requests if path.endswith('/SendAgentMessage')]
            self.assertEqual(len(send_calls), 1)
            self.assertEqual(send_calls[0]['recipient'], self.sid)
            self.assertEqual(send_calls[0]['senderId'], 'agent-bridge:demo/naru')
            self.assertIn(msg['id'], send_calls[0]['content'])

            wake_calls = [body for path, _, body in fake.requests if path.endswith('/SendUserCascadeMessage')]
            self.assertEqual(len(wake_calls), 2)
            self.assertEqual(wake_calls[0], {
                'cascadeId': self.sid,
                'deliveryStrategy': 'MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION',
            })

            again = self.cli(server.path, env, 'send', '--project', 'demo',
                             '--from', 'naru', '--to', 'jinni', '--key', 'k1', '--file', '-')
            self.assertEqual(again.returncode, 0)
            send_calls_after = [body for path, _, body in fake.requests if path.endswith('/SendAgentMessage')]
            self.assertEqual(len(send_calls_after), 1)

    def test_send_wakes_session_that_yields_to_idle_with_background_task(self):
        with FakeAgyServer(self.root, self.sid, send_behavior='finish-after-first-wake') as fake, RunningBroker() as server:
            env = self.env(fake)
            self.cli(server.path, env, 'register', 'jinni', '--project', 'demo',
                     '--transport', 'agy', '--session', self.sid, '--discover')
            server.call('register', project='demo', name='naru', cwd=str(self.root))

            sent = self.cli(server.path, env, 'send', '--project', 'demo',
                            '--from', 'naru', '--to', 'jinni', '--key', 'k-wake', '--file', '-')
            self.assertEqual(sent.returncode, 0, sent.stdout)
            self.assertEqual(fake.trajectories[self.sid]['status'], 'CASCADE_RUN_STATUS_RUNNING')
            wake_calls = [body for path, _, body in fake.requests if path.endswith('/SendUserCascadeMessage')]
            self.assertEqual(len(wake_calls), 2)

    def test_pre_send_missing_session_stays_pending_and_exits_nonzero(self):
        with FakeAgyServer(self.root, self.sid) as fake, RunningBroker() as server:
            env = self.env(fake)
            self.cli(server.path, env, 'register', 'jinni', '--project', 'demo',
                     '--transport', 'agy', '--session', self.sid, '--discover')
            server.call('register', project='demo', name='naru', cwd=str(self.root))
            # Remove session before send so pre-verification fails.
            fake.trajectories.pop(self.sid)
            res = self.cli(server.path, env, 'send', '--project', 'demo',
                           '--from', 'naru', '--to', 'jinni', '--key', 'k-miss', '--file', '-')
            self.assertEqual(res.returncode, 1)
            out = json.loads(res.stdout)
            self.assertTrue(out['push_failed'])
            self.assertEqual(server.call('show', id=out['message']['id'])['state'], 'pending')

            # Dry-run after restoring session shows frame without sending or leaking CSRF.
            fake.trajectories[self.sid] = {
                'summary': 'Restored',
                'status': 'CASCADE_RUN_STATUS_IDLE',
                'workspaces': [{'workspaceFolderAbsoluteUri': self.root.as_uri()}],
            }
            dry = self.cli(server.path, env, 'deliver', out['message']['id'], '--dry-run')
            self.assertEqual(dry.returncode, 0, dry.stdout)
            dry_json = json.loads(dry.stdout)
            self.assertFalse(dry_json['sent'])
            self.assertEqual(dry_json['frame']['recipient'], self.sid)
            self.assertEqual(dry_json['wake_frame']['cascadeId'], self.sid)
            self.assertNotIn(CSRF, dry.stdout)

    def test_post_send_error_marks_message_uncertain_and_blocks_followup(self):
        with FakeAgyServer(self.root, self.sid, send_behavior='error') as fake, RunningBroker() as server:
            env = self.env(fake)
            self.cli(server.path, env, 'register', 'jinni', '--project', 'demo',
                     '--transport', 'agy', '--session', self.sid, '--discover')
            server.call('register', project='demo', name='naru', cwd=str(self.root))
            res = self.cli(server.path, env, 'send', '--project', 'demo',
                           '--from', 'naru', '--to', 'jinni', '--key', 'k-err', '--file', '-')
            self.assertEqual(res.returncode, 1)
            out = json.loads(res.stdout)
            self.assertEqual(server.call('show', id=out['message']['id'])['state'], 'uncertain')


if __name__ == '__main__':
    unittest.main()

