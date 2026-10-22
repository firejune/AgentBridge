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

CSRF = 'ca5f-' + 'ab' * 16


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
                    # ProtoJSON omits an empty map, as a fresh Desktop hub does.
                    self.respond(200, {'trajectorySummaries': parent.trajectories} if parent.trajectories else {})
                elif self.path.endswith('/GetConversationMetadata'):
                    cid = body.get('conversationId')
                    traj = parent.trajectories.get(cid)
                    if not traj:
                        self.respond(200, {'code': 'unknown', 'message': f'trajectory not found: {cid}'})
                    else:
                        self.respond(200, {'metadata': {'workspaces': traj['workspaces']}})
                elif self.path.endswith('/GetCascadeModelConfigData'):
                    self.respond(200, {
                        'clientModelConfigs': [
                            {
                                'label': 'Gemini 3.1 Pro (High)',
                                'modelOrAlias': {'model': 'MODEL_PLACEHOLDER_M418'},
                                'modelId': 'gemini-3.1-pro-high',
                            },
                            {
                                'label': 'Claude Opus 4.6 (Thinking)',
                                'modelOrAlias': {'model': 'MODEL_PLACEHOLDER_M26'},
                                'modelId': 'claude-opus-4-6-thinking',
                            },
                        ],
                        'defaultOverrideModelConfig': {
                            'modelOrAlias': {'model': 'MODEL_PLACEHOLDER_M418'},
                        },
                    })
                elif self.path.endswith('/StartCascade'):
                    new_id = str(uuid.uuid4())
                    uris = body.get('workspaceUris') or []
                    proj_env = body.get('projectEnvConfig') or {}
                    proj_id = proj_env.get('projectId')
                    if proj_id and not uris:
                        uris = [parent.root.as_uri()]
                    parent.trajectories[new_id] = {
                        'summary': '',
                        'status': 'CASCADE_RUN_STATUS_IDLE',
                        'workspaces': [{'workspaceFolderAbsoluteUri': u} for u in uris],
                        'lastModifiedTime': '2026-10-18T12:00:00Z',
                    }
                    if proj_id:
                        parent.trajectories[new_id]['annotations'] = {'projectId': proj_id}
                    self.respond(200, {'cascadeId': new_id})
                elif self.path.endswith('/UpdateConversationAnnotations'):
                    cid = body.get('cascadeId')
                    traj = parent.trajectories.get(cid)
                    if traj is not None:
                        ann = body.get('annotations') or {}
                        if 'title' in ann:
                            traj['summary'] = ann['title']
                    self.respond(200, {})
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

    def cli(self, server_path, env, *args, stdin='Question for peer'):
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
            reg = self.cli(server.path, env, 'register', 'agy-agent', '--project', 'demo',
                           '--transport', 'agy', '--session', self.sid, '--discover')
            self.assertEqual(reg.returncode, 0, reg.stdout)
            self.assertEqual(json.loads(reg.stdout)['cwd'], str(self.root))
            server.call('register', project='demo', name='codex-agent', cwd=str(self.root))

            sent = self.cli(server.path, env, 'send', '--project', 'demo',
                            '--from', 'codex-agent', '--to', 'agy-agent', '--key', 'k1', '--file', '-')
            self.assertEqual(sent.returncode, 0, sent.stdout)
            msg = json.loads(sent.stdout)
            self.assertEqual(msg['state'], 'submitted')
            self.assertEqual(msg['events'][-1]['event'], 'pushed')
            self.assertNotIn(CSRF, sent.stdout + sent.stderr)

            send_calls = [body for path, _, body in fake.requests if path.endswith('/SendAgentMessage')]
            self.assertEqual(len(send_calls), 1)
            self.assertEqual(send_calls[0]['recipient'], self.sid)
            self.assertEqual(send_calls[0]['senderId'], 'agent-bridge:demo/codex-agent')
            self.assertIn(msg['id'], send_calls[0]['content'])

            wake_calls = [body for path, _, body in fake.requests if path.endswith('/SendUserCascadeMessage')]
            self.assertEqual(len(wake_calls), 2)
            self.assertEqual(wake_calls[0], {
                'cascadeId': self.sid,
                'deliveryStrategy': 'MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION',
            })

            again = self.cli(server.path, env, 'send', '--project', 'demo',
                             '--from', 'codex-agent', '--to', 'agy-agent', '--key', 'k1', '--file', '-')
            self.assertEqual(again.returncode, 0)
            send_calls_after = [body for path, _, body in fake.requests if path.endswith('/SendAgentMessage')]
            self.assertEqual(len(send_calls_after), 1)

    def test_send_wakes_session_that_yields_to_idle_with_background_task(self):
        with FakeAgyServer(self.root, self.sid, send_behavior='finish-after-first-wake') as fake, RunningBroker() as server:
            env = self.env(fake)
            self.cli(server.path, env, 'register', 'agy-agent', '--project', 'demo',
                     '--transport', 'agy', '--session', self.sid, '--discover')
            server.call('register', project='demo', name='codex-agent', cwd=str(self.root))

            sent = self.cli(server.path, env, 'send', '--project', 'demo',
                            '--from', 'codex-agent', '--to', 'agy-agent', '--key', 'k-wake', '--file', '-')
            self.assertEqual(sent.returncode, 0, sent.stdout)
            self.assertEqual(fake.trajectories[self.sid]['status'], 'CASCADE_RUN_STATUS_RUNNING')
            wake_calls = [body for path, _, body in fake.requests if path.endswith('/SendUserCascadeMessage')]
            self.assertEqual(len(wake_calls), 2)

    def test_pre_send_missing_session_stays_pending_and_exits_nonzero(self):
        with FakeAgyServer(self.root, self.sid) as fake, RunningBroker() as server:
            env = self.env(fake)
            self.cli(server.path, env, 'register', 'agy-agent', '--project', 'demo',
                     '--transport', 'agy', '--session', self.sid, '--discover')
            server.call('register', project='demo', name='codex-agent', cwd=str(self.root))
            # Remove session before send so pre-verification fails.
            fake.trajectories.pop(self.sid)
            res = self.cli(server.path, env, 'send', '--project', 'demo',
                           '--from', 'codex-agent', '--to', 'agy-agent', '--key', 'k-miss', '--file', '-')
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
            self.cli(server.path, env, 'register', 'agy-agent', '--project', 'demo',
                     '--transport', 'agy', '--session', self.sid, '--discover')
            server.call('register', project='demo', name='codex-agent', cwd=str(self.root))
            res = self.cli(server.path, env, 'send', '--project', 'demo',
                           '--from', 'codex-agent', '--to', 'agy-agent', '--key', 'k-err', '--file', '-')
            self.assertEqual(res.returncode, 1)
            out = json.loads(res.stdout)
            self.assertEqual(server.call('show', id=out['message']['id'])['state'], 'uncertain')

    def test_open_and_register_new_create_desktop_session_with_default_agent_spec(self):
        with FakeAgyServer(self.root, self.sid) as fake, RunningBroker() as server:
            config_dir = self.root / 'gemini-config'
            projects_dir = config_dir / 'projects'
            projects_dir.mkdir(parents=True)
            proj_id = str(uuid.uuid4())
            (projects_dir / f'{proj_id}.json').write_text(
                json.dumps({'id': proj_id, 'name': 'demo-proj', 'workspacePaths': [str(self.root)]}),
                encoding='utf-8',
            )
            env = {**self.env(fake), 'GEMINI_CONFIG_DIR': str(config_dir)}
            sock = self.root / 'unused.sock'

            # 1) Standalone open with project matching, custom title, and model alias.
            opened = self.cli(sock, env, 'open', '--provider', 'agy', '--cwd', str(self.root),
                              '--title', 'Fresh Session', '--model', 'claude-opus-4-6-thinking')
            self.assertEqual(opened.returncode, 0, opened.stdout)
            opened_json = json.loads(opened.stdout)
            new_sid = opened_json['session']['id']
            self.assertTrue(opened_json['created'])
            self.assertEqual(opened_json['session']['name'], 'Fresh Session')
            self.assertEqual(opened_json['session']['cwd'], str(self.root))
            self.assertEqual(opened_json['model'], 'claude-opus-4-6-thinking')
            self.assertEqual(opened_json['model_enum'], 'MODEL_PLACEHOLDER_M26')
            self.assertEqual(opened_json['project_id'], proj_id)

            start_calls = [body for path, _, body in fake.requests if path.endswith('/StartCascade')]
            self.assertEqual(len(start_calls), 1)
            self.assertEqual(start_calls[0]['projectEnvConfig']['projectId'], proj_id)
            self.assertEqual(
                start_calls[0]['customAgentSpec']['cascadeConfig']['plannerConfig']['planModel'],
                'MODEL_PLACEHOLDER_M26',
            )
            self.assertTrue(
                start_calls[0]['customAgentSpec']['builtinAgent']['defaultAgent']['isInteractive']
            )

            # 2) register --new opens and immediately binds the new session for messaging.
            unregistered_dir = self.root / 'subworkspace'
            unregistered_dir.mkdir()
            reg = self.cli(server.path, env, 'register', 'agy-fresh', '--project', 'demo',
                           '--transport', 'agy', '--new', '--cwd', str(unregistered_dir),
                           '--title', 'Subworkspace Worker')
            self.assertEqual(reg.returncode, 0, reg.stdout)
            reg_json = json.loads(reg.stdout)
            fresh_sid = reg_json['session']
            self.assertEqual(reg_json['cwd'], str(unregistered_dir))
            self.assertEqual(reg_json['discovery']['session']['name'], 'Subworkspace Worker')

            server.call('register', project='demo', name='codex-agent', cwd=str(self.root))
            sent = self.cli(server.path, env, 'send', '--project', 'demo',
                            '--from', 'codex-agent', '--to', 'agy-fresh', '--key', 'k-fresh', '--file', '-')
            self.assertEqual(sent.returncode, 0, sent.stdout)
            self.assertEqual(json.loads(sent.stdout)['state'], 'submitted')
            self.assertEqual(fake.trajectories[fresh_sid]['status'], 'CASCADE_RUN_STATUS_RUNNING')

            # 3) Unknown model or missing directory fails loudly.
            bad_model = self.cli(sock, env, 'open', '--cwd', str(self.root), '--model', 'nonexistent-model')
            self.assertEqual(bad_model.returncode, 1)
            self.assertIn('Unknown Antigravity model', json.loads(bad_model.stdout)['error'])

    def test_discovers_both_desktop_and_ide_servers_while_skipping_lsp_workers(self):
        from unittest import mock
        import agy_live

        desktop_sid = str(uuid.uuid4())
        ide_sid = str(uuid.uuid4())
        with FakeAgyServer(self.root, desktop_sid) as desktop_srv, FakeAgyServer(self.root, ide_sid) as ide_srv:
            uid = os.getuid()
            ps_text = (
                f"1001 {uid} /Applications/Antigravity.app/Contents/Resources/bin/language_server --csrf_token {CSRF} --app_data_dir antigravity\n"
                f"1002 {uid} /Applications/Antigravity IDE.app/Contents/Resources/app/extensions/antigravity/bin/language_server_macos_arm --csrf_token {CSRF} --app_data_dir antigravity-ide --subclient_type ide\n"
                f"1003 {uid} /Applications/Antigravity IDE.app/Contents/Resources/app/extensions/antigravity/bin/language_server_macos_arm --enable_lsp --csrf_token {CSRF} --app_data_dir antigravity-ide\n"
            )
            desktop_port = desktop_srv.httpd.server_port
            ide_port = ide_srv.httpd.server_port

            def fake_check_output(cmd, *args, **kwargs):
                if cmd[0] == 'ps':
                    return ps_text
                if cmd[0] == 'lsof':
                    pid = cmd[cmd.index('-p') + 1]
                    port = desktop_port if pid == '1001' else ide_port
                    return f"language_ {pid} user 10u IPv4 0x0 0t0 TCP 127.0.0.1:{port} (LISTEN)\n"
                raise AssertionError(f"Unexpected command: {cmd}")

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('AGENT_BRIDGE_AGY_URL', None)
                os.environ.pop('AGENT_BRIDGE_AGY_CSRF', None)
                with mock.patch('subprocess.check_output', side_effect=fake_check_output):
                    listed = agy_live.list_agy(cwd=str(self.root))
                    by_id = {row['id']: row for row in listed['sessions']}
                    self.assertIn(desktop_sid, by_id)
                    self.assertIn(ide_sid, by_id)
                    self.assertIn('desktop', by_id[desktop_sid]['scope'])
                    self.assertIn('IDE', by_id[ide_sid]['scope'])

                    srv_ide, row_ide = agy_live.resolve_agy(ide_sid)
                    self.assertEqual(srv_ide['app_dir'], 'antigravity-ide')
                    self.assertEqual(row_ide['id'], ide_sid)


    def test_desktop_hub_without_conversations_is_an_empty_registry(self):
        from unittest import mock
        import agy_live

        with FakeAgyServer(self.root, str(uuid.uuid4())) as desktop_srv:
            desktop_srv.trajectories.clear()
            uid = os.getuid()
            ps_text = (f"1001 {uid} /Applications/Antigravity.app/Contents/Resources/bin/language_server "
                       f"--standalone --subclient_type hub --csrf_token {CSRF} --app_data_dir antigravity\n")
            port = desktop_srv.httpd.server_port

            def fake_check_output(cmd, *args, **kwargs):
                if cmd[0] == 'ps':
                    return ps_text
                if cmd[0] == 'lsof':
                    return f"language_ 1001 user 10u IPv4 0x0 0t0 TCP 127.0.0.1:{port} (LISTEN)\n"
                raise AssertionError(f"Unexpected command: {cmd}")

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('AGENT_BRIDGE_AGY_URL', None)
                os.environ.pop('AGENT_BRIDGE_AGY_CSRF', None)
                with mock.patch('subprocess.check_output', side_effect=fake_check_output):
                    server = agy_live.discover_desktop_server()
            self.assertEqual((server['app_dir'], server['summaries']), ('antigravity', {}))

    def test_non_map_summaries_are_still_refused(self):
        import agy_live
        self.assertEqual(agy_live.trajectory_summaries({}), {})
        self.assertIsNone(agy_live.trajectory_summaries({'trajectorySummaries': []}))

if __name__ == '__main__':
    unittest.main()


