import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
import sessions
from support import RunningBroker

CSRF = 'ca5f-' + 'ab' * 16


class FakeAgyServer:
    """Isolated local HTTP ConnectRPC stub for Antigravity language_server."""

    def __init__(self, root, sid, other_sid=None, send_behavior='ok', wake_behavior='ok'):
        self.root = Path(root).resolve()
        self.sid = sid
        self.other_sid = other_sid or str(uuid.uuid4())
        self.send_behavior = send_behavior
        self.wake_behavior = wake_behavior
        self.ignore_archive = False
        self.requests = []
        self.projects = {}
        self.environments = {}
        self.trajectory_steps = {}
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
                elif self.path.endswith('/CreateProject'):
                    proj = body.get('project') or {}
                    pid = proj.get('id')
                    if pid:
                        parent.projects[pid] = proj
                    self.respond(200, {})
                elif self.path.endswith('/AddEnvironmentToProject'):
                    pid = body.get('projectId')
                    env = body.get('environment') or {}
                    eid = env.get('id')
                    if pid and eid:
                        parent.environments[(pid, eid)] = env
                    self.respond(200, {})
                elif self.path.endswith('/StartCascade'):
                    new_id = str(uuid.uuid4())
                    uris = body.get('workspaceUris') or []
                    proj_env = body.get('projectEnvConfig') or {}
                    proj_id = proj_env.get('projectId')
                    env_id = proj_env.get('environmentId')
                    if proj_id and env_id and not uris:
                        env_obj = parent.environments.get((proj_id, env_id)) or {}
                        env_res = (env_obj.get('resources') or {}).get('resources') or []
                        uris = [r.get('folderUri') for r in env_res if isinstance(r, dict) and r.get('folderUri')]
                    if proj_id and not uris:
                        proj_obj = parent.projects.get(proj_id) or {}
                        proj_res = (proj_obj.get('projectResources') or {}).get('resources') or []
                        for r in proj_res:
                            if isinstance(r, dict):
                                u = r.get('folderUri') or (r.get('gitFolder') or {}).get('folderUri')
                                if u:
                                    uris.append(u)
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
                        meta = {'projectId': proj_id, 'workspaces': [{'workspaceFolderAbsoluteUri': u} for u in uris]}
                        if env_id:
                            meta['environmentId'] = env_id
                        parent.trajectories[new_id]['trajectoryMetadata'] = meta
                    self.respond(200, {'cascadeId': new_id})
                elif self.path.endswith('/UpdateConversationAnnotations'):
                    cid = body.get('cascadeId')
                    traj = parent.trajectories.get(cid)
                    if traj is not None:
                        ann = body.get('annotations') or {}
                        if 'title' in ann:
                            traj['summary'] = ann['title']
                        if 'archived' in ann and not parent.ignore_archive:
                            stored = traj.setdefault('annotations', {})
                            stored['archived'] = ann['archived']
                            stored['archivalStatusTimestamp'] = '2026-10-24T09:16:35Z'
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
                elif self.path.endswith('/GetCascadeTrajectorySteps'):
                    cid = body.get('cascadeId')
                    offset = int(body.get('stepOffset') or 0)
                    steps = parent.trajectory_steps.get(cid)
                    if steps is None:
                        traj = parent.trajectories.get(cid) or {}
                        steps = traj.get('steps') or []
                    self.respond(200, {'steps': steps[offset:] if 0 <= offset <= len(steps) else []})
                elif self.path.endswith('/SendUserCascadeMessage'):
                    cid = body.get('cascadeId')
                    traj = parent.trajectories.get(cid)
                    if not traj:
                        self.respond(500, {'code': 'unknown', 'message': f'trajectory not found: {cid}'})
                    elif parent.wake_behavior == 'error':
                        self.respond(500, {'code': 'internal', 'message': 'wake transport error'})
                    elif parent.wake_behavior == 'noop-idle':
                        self.respond(200, {})
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

            # 4) Omitting --title falls back to participant name on register --new,
            #    workspace folder name on open, and first message line when untitled.
            reg_fallback = self.cli(server.path, env, 'register', 'jinni-desk-76', '--project', 'demo',
                                    '--transport', 'agy', '--new', '--cwd', str(unregistered_dir))
            self.assertEqual(reg_fallback.returncode, 0, reg_fallback.stdout)
            self.assertEqual(json.loads(reg_fallback.stdout)['discovery']['session']['name'], 'jinni-desk-76')

            open_fallback = self.cli(sock, env, 'open', '--provider', 'agy', '--cwd', str(unregistered_dir))
            self.assertEqual(open_fallback.returncode, 0, open_fallback.stdout)
            open_fallback_json = json.loads(open_fallback.stdout)
            self.assertEqual(open_fallback_json['session']['name'], 'subworkspace')

            untitled_sid = open_fallback_json['session']['id']
            fake.trajectories[untitled_sid]['summary'] = ''
            self.cli(server.path, env, 'register', 'agy-untitled', '--project', 'demo',
                     '--transport', 'agy', '--session', untitled_sid, '--discover')
            sent_untitled = self.cli(
                server.path, env, 'send', '--project', 'demo',
                '--from', 'codex-agent', '--to', 'agy-untitled', '--key', 'k-untitled', '--file', '-',
                stdin='# Card: rig-c/adapters#76 — phaser, three\nDetails here',
            )
            self.assertEqual(sent_untitled.returncode, 0, sent_untitled.stdout)
            self.assertEqual(fake.trajectories[untitled_sid]['summary'], 'Card: rig-c/adapters#76 — phaser, three')

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

    def ide_servers(self, fake, desktop=False):
        rows = [{'pid': 2002, 'app_dir': 'antigravity-ide', 'url': fake.url, 'csrf': CSRF, 'summaries': {}}]
        if desktop:
            rows.insert(0, {'pid': 2001, 'app_dir': 'antigravity', 'url': 'http://127.0.0.1:1', 'csrf': CSRF, 'summaries': {}})
        return rows

    def test_open_host_ide_and_desktop_create_with_identical_project_association(self):
        import agy_live
        with FakeAgyServer(self.root, self.sid) as fake, \
                mock.patch.object(agy_live, 'discover_servers', return_value=self.ide_servers(fake, desktop=True)), \
                mock.patch.object(agy_live, 'find_desktop_project_id', return_value=None):
            opened = sessions.open_session('agy', cwd=str(self.root), title='IDE Worker', host='ide')
        self.assertEqual((opened['created'], opened['host']), (True, 'ide'))
        self.assertIsNotNone(opened['project_id'])
        self.assertEqual((opened['session']['cwd'], opened['session']['name']), (str(self.root), 'IDE Worker'))
        start = [body for path, _, body in fake.requests if path.endswith('/StartCascade')]
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0]['projectEnvConfig']['projectId'], opened['project_id'])

    def test_host_ide_and_desktop_share_unified_server_fallback(self):
        import agy_live
        desktop_only = [{'pid': 2001, 'app_dir': 'antigravity', 'url': 'http://127.0.0.1:1', 'csrf': CSRF, 'summaries': {}}]
        with mock.patch.object(agy_live, 'discover_servers', return_value=desktop_only):
            self.assertEqual(agy_live.discover_host_server('ide')['pid'], 2001)
            self.assertEqual(agy_live.discover_desktop_server()['app_dir'], 'antigravity')
        with mock.patch.object(agy_live, 'discover_servers', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'IDE language_server is not running'):
                agy_live.discover_host_server('ide')
        with self.assertRaisesRegex(ValueError, 'Unknown Antigravity host'):
            agy_live.discover_host_server('cloud')

    def test_ide_flag_is_claude_only_and_host_reaches_agy(self):
        with self.assertRaisesRegex(ValueError, '--ide picks the IDE of a Claude Code session'):
            sessions.open_session('agy', cwd=str(self.root), host='ide', ide='vscode')
        with mock.patch.object(sessions.agy_live, 'open_agy', return_value='ok') as opened:
            self.assertEqual(sessions.open_session('agy', cwd=str(self.root), host='ide'), 'ok')
            sessions.open_session('agy', cwd=str(self.root))
        self.assertEqual([c.kwargs['host'] for c in opened.call_args_list], ['ide', 'desktop'])

    def test_non_map_summaries_are_still_refused(self):
        import agy_live
        self.assertEqual(agy_live.trajectory_summaries({}), {})
        self.assertIsNone(agy_live.trajectory_summaries({'trajectorySummaries': []}))
        self.assertIsNone(agy_live.trajectory_summaries({'code': 'internal', 'message': 'registry unavailable'}))

    def test_unified_agy_hub_ide_windows_coexist_with_desktop_and_pick_freshest_owner(self):
        import agy_live

        shared_sid = str(uuid.uuid4())
        desktop_sid = str(uuid.uuid4())
        win_a = self.root / 'win-a'
        win_b = self.root / 'win-b'
        win_a.mkdir()
        win_b.mkdir()

        with FakeAgyServer(self.root, desktop_sid) as desktop_srv, \
                FakeAgyServer(win_a, str(uuid.uuid4())) as ide_a_srv, \
                FakeAgyServer(win_b, shared_sid) as ide_b_srv:
            # Desktop holds an older idle snapshot (stepCount=6) of shared_sid,
            # while IDE window B holds the newer progress (stepCount=68).
            desktop_srv.trajectories[shared_sid] = {
                'summary': 'Unified Conversation',
                'stepCount': 6,
                'status': 'CASCADE_RUN_STATUS_IDLE',
                'workspaces': [{'workspaceFolderAbsoluteUri': win_b.as_uri()}],
                'lastModifiedTime': '2026-10-28T10:48:40Z',
            }
            ide_b_srv.trajectories[shared_sid] = {
                'summary': 'Unified Conversation',
                'stepCount': 68,
                'status': 'CASCADE_RUN_STATUS_IDLE',
                'workspaces': [{'workspaceFolderAbsoluteUri': win_b.as_uri()}],
                'lastModifiedTime': '2026-10-28T10:55:26Z',
            }
            uid = os.getuid()
            p_desk = desktop_srv.httpd.server_port
            p_a = ide_a_srv.httpd.server_port
            p_b = ide_b_srv.httpd.server_port
            ps_text = (
                f"3001 {uid} /Applications/Antigravity.app/Contents/Resources/bin/language_server "
                f"--standalone --subclient_type hub --csrf_token {CSRF} --app_data_dir antigravity\n"
                f"3002 {uid} /Users/test/.gemini/bin/agy --hub --hub-port={p_a} "
                f"--app_data_dir=antigravity --csrf_token={CSRF} --add-dir={win_a}\n"
                f"3003 {uid} /Users/test/.gemini/bin/agy --hub --hub-port={p_b} "
                f"--app_data_dir=antigravity --csrf_token={CSRF} --add-dir={win_b}\n"
            )
            ports_by_pid = {'3001': p_desk, '3002': p_a, '3003': p_b}

            def fake_check_output(cmd, *args, **kwargs):
                if cmd[0] == 'ps':
                    return ps_text
                if cmd[0] == 'lsof':
                    pid = cmd[cmd.index('-p') + 1]
                    return f"agy {pid} user 10u IPv4 0x0 0t0 TCP 127.0.0.1:{ports_by_pid[pid]} (LISTEN)\n"
                raise AssertionError(f"Unexpected command: {cmd}")

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('AGENT_BRIDGE_AGY_URL', None)
                os.environ.pop('AGENT_BRIDGE_AGY_CSRF', None)
                with mock.patch('subprocess.check_output', side_effect=fake_check_output):
                    listed = agy_live.list_agy()
                    by_id = {row['id']: row for row in listed['sessions']}
                    self.assertEqual(by_id[shared_sid]['pid'], 3003)
                    self.assertIn('IDE', by_id[shared_sid]['scope'])

                    srv, row = agy_live.resolve_agy(shared_sid)
                    self.assertEqual((srv['pid'], srv['host'], row['pid']), (3003, 'ide', 3003))
                    # Resolving an in-summary session must not trigger GetConversationMetadata on other servers.
                    meta_calls_a = [p for p, _, _ in ide_a_srv.requests if p.endswith('/GetConversationMetadata')]
                    self.assertEqual(meta_calls_a, [])

                    # discover_host_server('ide', cwd=...) selects the matching window's agy --hub server.
                    matched_srv = agy_live.discover_host_server('ide', cwd=str(win_b))
                    self.assertEqual(matched_srv['pid'], 3003)

    def test_orphaned_agy_hub_is_ignored_for_host_ide_and_worktrees_match_repo(self):
        import agy_live

        idle_sid = str(uuid.uuid4())
        running_sid = str(uuid.uuid4())
        repo_a = self.root / 'repo-a'
        repo_b = self.root / 'repo-b'
        wt_b = self.root / '.worktrees' / 'repo-b' / 'jinni-52'
        (repo_b / '.git' / 'worktrees' / 'jinni-52').mkdir(parents=True)
        repo_a.mkdir()
        wt_b.mkdir(parents=True)
        (wt_b / '.git').write_text(f"gitdir: {repo_b / '.git' / 'worktrees' / 'jinni-52'}\n", encoding='utf-8')

        with FakeAgyServer(self.root, str(uuid.uuid4())) as orphan_srv, \
                FakeAgyServer(repo_a, str(uuid.uuid4())) as ide_a_srv, \
                FakeAgyServer(repo_b, str(uuid.uuid4())) as ide_b_srv:
            orphan_srv.trajectories.clear()
            # Orphan holds a higher stepCount for idle_sid, while live ide_b_srv holds a lower stepCount.
            orphan_srv.trajectories[idle_sid] = {
                'summary': 'Idle Worker',
                'stepCount': 460,
                'status': 'CASCADE_RUN_STATUS_IDLE',
                'workspaces': [{'workspaceFolderAbsoluteUri': wt_b.as_uri()}],
                'lastModifiedTime': '2026-10-31T11:03:38Z',
            }
            ide_b_srv.trajectories[idle_sid] = {
                'summary': 'Idle Worker',
                'stepCount': 448,
                'status': 'CASCADE_RUN_STATUS_IDLE',
                'workspaces': [{'workspaceFolderAbsoluteUri': wt_b.as_uri()}],
                'lastModifiedTime': '2026-10-31T10:52:03Z',
            }
            # Orphan is still actively running running_sid.
            orphan_srv.trajectories[running_sid] = {
                'summary': 'Running Worker',
                'stepCount': 633,
                'status': 'CASCADE_RUN_STATUS_RUNNING',
                'workspaces': [{'workspaceFolderAbsoluteUri': wt_b.as_uri()}],
                'lastModifiedTime': '2026-10-31T11:05:07Z',
            }
            ide_b_srv.trajectories[running_sid] = {
                'summary': 'Running Worker',
                'stepCount': 592,
                'status': 'CASCADE_RUN_STATUS_IDLE',
                'workspaces': [{'workspaceFolderAbsoluteUri': wt_b.as_uri()}],
                'lastModifiedTime': '2026-10-31T10:52:59Z',
            }

            uid = os.getuid()
            p_orphan = orphan_srv.httpd.server_port
            p_a = ide_a_srv.httpd.server_port
            p_b = ide_b_srv.httpd.server_port
            ps_text = (
                f"4000 1 {uid} /Users/test/.gemini/bin/agy --hub --hub-port={p_orphan} "
                f"--app_data_dir=antigravity --csrf_token={CSRF}\n"
                f"4001 79989 {uid} /Users/test/.gemini/bin/agy --hub --hub-port={p_a} "
                f"--app_data_dir=antigravity --csrf_token={CSRF} --add-dir={repo_a}\n"
                f"4002 79990 {uid} /Users/test/.gemini/bin/agy --hub --hub-port={p_b} "
                f"--app_data_dir=antigravity --csrf_token={CSRF} --add-dir={repo_b}\n"
            )
            ports_by_pid = {'4000': p_orphan, '4001': p_a, '4002': p_b}

            def fake_check_output(cmd, *args, **kwargs):
                if cmd[0] == 'ps':
                    return ps_text
                if cmd[0] == 'lsof':
                    pid = cmd[cmd.index('-p') + 1]
                    return f"agy {pid} user 10u IPv4 0x0 0t0 TCP 127.0.0.1:{ports_by_pid[pid]} (LISTEN)\n"
                raise AssertionError(f"Unexpected command: {cmd}")

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('AGENT_BRIDGE_AGY_URL', None)
                os.environ.pop('AGENT_BRIDGE_AGY_CSRF', None)
                with mock.patch('subprocess.check_output', side_effect=fake_check_output), \
                        mock.patch('os.kill') as fake_kill:
                    # 1) Unmatched workspace skips orphaned pid 4000 (ppid=1) and picks first live IDE server (4001).
                    unmatched_srv = agy_live.discover_host_server('ide', cwd=str(self.root))
                    self.assertEqual(unmatched_srv['pid'], 4001)

                    # 2) Worktree wt_b resolves its .git file to repo_b and selects pid 4002.
                    wt_srv = agy_live.discover_host_server('ide', cwd=str(wt_b))
                    self.assertEqual(wt_srv['pid'], 4002)

                    # 3) Idle session prefers live server (4002) over orphaned server (4000).
                    srv_idle, row_idle = agy_live.resolve_agy(idle_sid)
                    self.assertEqual((srv_idle['pid'], row_idle['pid']), (4002, 4002))

                    # 4) Actively running session still resolves to the running server (4000) and is not killed.
                    srv_run, row_run = agy_live.resolve_agy(running_sid)
                    self.assertEqual((srv_run['pid'], row_run['status']), (4000, 'running'))
                    fake_kill.assert_not_called()

                    # 5) Once the orphan has no running trajectories, discover_servers() sends SIGTERM and drops it.
                    orphan_srv.trajectories[running_sid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
                    discovered = agy_live.discover_servers()
                    self.assertEqual([s['pid'] for s in discovered], [4001, 4002])
                    fake_kill.assert_called_once_with(4000, agy_live.signal.SIGTERM)

    def test_lsof_missing_falls_back_to_hub_port_and_desktop_host_uses_unified_hub(self):
        import agy_live

        sid = str(uuid.uuid4())
        with FakeAgyServer(self.root, sid) as hub_srv:
            uid = os.getuid()
            port = hub_srv.httpd.server_port
            ps_text = (
                f"5001 79990 {uid} /Users/test/.gemini/bin/agy --hub --hub-port={port} "
                f"--app_data_dir=antigravity --csrf_token={CSRF} --add-dir={self.root}\n"
            )

            def fake_check_output(cmd, *args, **kwargs):
                if cmd[0] == 'ps':
                    return ps_text
                raise FileNotFoundError(2, 'No such file or directory', cmd[0])

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('AGENT_BRIDGE_AGY_URL', None)
                os.environ.pop('AGENT_BRIDGE_AGY_CSRF', None)
                with mock.patch('shutil.which', return_value=None), \
                        mock.patch('subprocess.check_output', side_effect=fake_check_output):
                    listed = agy_live.list_agy(cwd=str(self.root))
                    self.assertEqual([row['id'] for row in listed['sessions']], [sid])
                    desktop_srv = agy_live.discover_host_server('desktop', cwd=str(self.root))
                    self.assertEqual(desktop_srv['pid'], 5001)

    def test_worktree_and_unified_hub_open_associate_project_and_environment(self):
        import agy_live

        config_dir = self.root / 'gemini-config'
        projects_dir = config_dir / 'projects'
        projects_dir.mkdir(parents=True)

        repo_a = self.root / 'repo-a'
        wt_a = self.root / '.worktrees' / 'repo-a' / 'jinni-71'
        (repo_a / '.git' / 'worktrees' / 'jinni-71').mkdir(parents=True)
        wt_a.mkdir(parents=True)
        (wt_a / '.git').write_text(f"gitdir: {repo_a / '.git' / 'worktrees' / 'jinni-71'}\n", encoding='utf-8')

        proj_a = str(uuid.uuid4())
        (projects_dir / f'{proj_a}.json').write_text(
            json.dumps({
                'id': proj_a,
                'name': 'repo-a',
                'projectResources': {
                    'resources': [{'gitFolder': {'folderUri': repo_a.as_uri()}}],
                },
            }),
            encoding='utf-8',
        )

        # Unregistered git repo and its worktree
        repo_b = self.root / 'repo-b'
        wt_b = self.root / '.worktrees' / 'repo-b' / 'jinni-80'
        (repo_b / '.git' / 'worktrees' / 'jinni-80').mkdir(parents=True)
        wt_b.mkdir(parents=True)
        (wt_b / '.git').write_text(f"gitdir: {repo_b / '.git' / 'worktrees' / 'jinni-80'}\n", encoding='utf-8')

        with FakeAgyServer(repo_a, str(uuid.uuid4())) as hub_srv:
            unified_rows = [{
                'pid': 6001,
                'ppid': 6000,
                'orphaned': False,
                'app_dir': 'antigravity',
                'host': 'ide',
                'add_dirs': (str(repo_a),),
                'url': hub_srv.url,
                'csrf': CSRF,
                'summaries': hub_srv.trajectories,
            }]
            with mock.patch.dict(os.environ, {'GEMINI_CONFIG_DIR': str(config_dir)}, clear=False), \
                    mock.patch.object(agy_live, 'discover_servers', return_value=unified_rows):
                # 1) Opening a worktree of registered repo_a on a unified hub (host='ide')
                #    resolves proj_a, registers a worktree environment, and keeps wt_a as cwd.
                opened_wt = agy_live.open_agy(str(wt_a), title='jinni-desk-71', host='ide')
                self.assertEqual(opened_wt['project_id'], proj_a)
                self.assertEqual(opened_wt['session']['cwd'], str(wt_a))
                add_env_calls = [b for p, _, b in hub_srv.requests if p.endswith('/AddEnvironmentToProject')]
                self.assertEqual(len(add_env_calls), 1)
                self.assertEqual(add_env_calls[0]['projectId'], proj_a)
                env_id = add_env_calls[0]['environment']['id']

                start_calls = [b for p, _, b in hub_srv.requests if p.endswith('/StartCascade')]
                self.assertEqual(
                    start_calls[-1]['projectEnvConfig'],
                    {'projectId': proj_a, 'environmentId': env_id},
                )

                # 2) Opening a worktree of unregistered git repo_b auto-creates a project
                #    for repo_b and attaches the worktree environment.
                opened_unreg = agy_live.open_agy(str(wt_b), title='jinni-desk-80', host='desktop')
                create_proj_calls = [b for p, _, b in hub_srv.requests if p.endswith('/CreateProject')]
                self.assertEqual(len(create_proj_calls), 1)
                created_proj = create_proj_calls[0]['project']
                self.assertEqual(created_proj['name'], 'repo-b')
                self.assertEqual(
                    created_proj['projectResources']['resources'][0]['gitFolder']['folderUri'],
                    repo_b.as_uri(),
                )
                self.assertEqual(opened_unreg['project_id'], created_proj['id'])
                self.assertEqual(opened_unreg['session']['cwd'], str(wt_b))


FAILURE_FIXTURE = Path(__file__).with_name('fixtures') / 'agy-provider-failure.json'


def load_failure_fixture():
    return json.loads(FAILURE_FIXTURE.read_text(encoding='utf-8'))


class AgyProviderFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-agy-fail-', dir='/tmp')
        self.root = Path(self.temp.name).resolve()
        self.fixture = load_failure_fixture()
        self.sid = self.fixture['cascadeId']

    def tearDown(self):
        self.temp.cleanup()

    def make_timeout_step(self, step_index, created_at='2026-11-08T12:04:00Z', status='CORTEX_STEP_STATUS_DONE'):
        step = json.loads(json.dumps(self.fixture['terminal_timeout_step']))
        step['status'] = status
        step['metadata']['createdAt'] = created_at
        step['errorMessage']['error']['errorId'] = f'00000000-0000-4000-8000-000000000900-{step_index}'
        return step

    def test_fixture_is_synthetic_and_classifies_two_match_classes_and_guards(self):
        import agy_live

        raw_text = FAILURE_FIXTURE.read_text(encoding='utf-8')
        self.assertNotIn('/Users/', raw_text)
        self.assertNotIn('firejune', raw_text)

        # 1) Transient internal attempt (attempt 1-8 without shouldShowUser) neither wakes nor notifies.
        transient = agy_live.classify_tail_steps([self.fixture['transient_attempt_step']], 18, sid=self.sid)
        self.assertEqual(transient['outcome'], 'transient_attempt')

        # 2) Terminal shouldShowUser step carrying "http2: timeout awaiting response headers" -> wake class.
        wake = agy_live.classify_tail_steps([self.fixture['terminal_timeout_step']], 19, sid=self.sid)
        self.assertEqual(wake['outcome'], 'wake')
        self.assertEqual(wake['kind'], 'provider_header_timeout')
        self.assertEqual(wake['step_index'], 18)
        self.assertEqual(wake['turn_id'], 'agy:18')
        self.assertEqual(wake['failed_at'], '2026-11-08T12:04:00+00:00')

        # 3) Any other terminal shouldShowUser error step -> notify_only class.
        notify = agy_live.classify_tail_steps([self.fixture['terminal_notify_only_step']], 20, sid=self.sid)
        self.assertEqual(notify['outcome'], 'notify_only')
        self.assertEqual(notify['kind'], 'terminal_error')
        self.assertEqual(notify['step_index'], 19)
        self.assertEqual(notify['turn_id'], 'agy:19')

        # 4) Non-error tail step -> non_error outcome (used to reset retry counter).
        non_err = agy_live.classify_tail_steps([self.fixture['non_error_step']], 21, sid=self.sid)
        self.assertEqual(non_err['outcome'], 'non_error')

        # 5) Trailing step, non-DONE status, or missing errorId on a wake-eligible error -> unsafe outcome.
        trailing = agy_live.classify_tail_steps(
            [self.fixture['terminal_timeout_step'], self.fixture['non_error_step']], 19, sid=self.sid,
        )
        self.assertEqual(trailing['outcome'], 'unsafe')
        running_step = self.make_timeout_step(18, status='CORTEX_STEP_STATUS_RUNNING')
        self.assertEqual(agy_live.classify_tail_steps([running_step], 19, sid=self.sid)['outcome'], 'unsafe')
        missing_id_step = self.make_timeout_step(18)
        missing_id_step['errorMessage']['error']['errorId'] = ''
        self.assertEqual(agy_live.classify_tail_steps([missing_id_step], 19, sid=self.sid)['outcome'], 'unsafe')

        # 6) Extending WAKE_ELIGIBLE_ERROR_SUBSTRINGS by one entry enables wake on a new transient signature.
        custom_step = json.loads(json.dumps(self.fixture['terminal_notify_only_step']))
        custom_step['errorMessage']['error']['shortError'] = 'agent executor error: upstream stream reset by peer'
        with mock.patch.object(
            agy_live,
            'WAKE_ELIGIBLE_ERROR_SUBSTRINGS',
            agy_live.WAKE_ELIGIBLE_ERROR_SUBSTRINGS + ('upstream stream reset by peer',),
        ):
            custom = agy_live.classify_tail_steps([custom_step], 20, sid=self.sid)
            self.assertEqual(custom['outcome'], 'wake')
            self.assertTrue(agy_live.valid_failure_kind(custom['kind']))

    def _make_state(self):
        import broker
        import route_store

        state_dir = Path(self.temp.name) / f'state-{uuid.uuid4().hex[:8]}'
        store = route_store.RouteStore(state_dir)
        self.addCleanup(store.close)
        state = broker.State(store=store)
        for name, transport, sid in (('parent', 'realtime', None), ('worker', 'agy', self.sid)):
            state.action(dict(op='register', project='demo', name=name, transport=transport,
                              session=sid, cwd=str(self.root)))
        source = state.action(dict(op='send', project='demo', sender='parent',
                                   recipient='worker', body='task payload', key='task-1'))
        state.action(dict(op='claim', id=source['id']))
        state.action(dict(op='finish', id=source['id'], state='submitted', event='pushed'))
        return state, store, source

    def test_monitor_polls_once_per_server_skips_running_and_fetches_tail_only_on_idle_change(self):
        import asyncio
        import agy_live
        import codex_failures
        import provider_failures

        state, _, source = self._make_state()
        state.action(dict(op='register', project='demo', name='worker-alias', transport='agy',
                          session=self.sid, cwd=str(self.root)))
        now = [100.0]
        monitor = agy_live.AgyRetryMonitor(state, provider_failures.retry_policy(), clock=lambda: now[0])

        with FakeAgyServer(self.root, self.sid) as fake:
            fake.trajectories[self.sid]['status'] = 'CASCADE_RUN_STATUS_RUNNING'
            fake.trajectories[self.sid]['stepCount'] = 18
            fake.trajectory_steps[self.sid] = [self.fixture['transient_attempt_step']] * 18

            with mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF}):
                # While RUNNING (attempts 1-8), GetAllCascadeTrajectories is polled once across both routes and tail step is not fetched.
                asyncio.run(monitor.check_routes())
                step_calls = [b for p, _, b in fake.requests if p.endswith('/GetCascadeTrajectorySteps')]
                traj_calls = [b for p, _, b in fake.requests if p.endswith('/GetAllCascadeTrajectories')]
                self.assertEqual(len(traj_calls), 1)
                self.assertEqual(len(step_calls), 0)
                self.assertIsNone(codex_failures.retry_notice(state, state.endpoints[('demo', 'worker')]))

                # Transition to IDLE at stepCount=19 with terminal timeout step.
                fake.trajectories[self.sid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
                fake.trajectories[self.sid]['stepCount'] = 19
                fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 18 + [self.fixture['terminal_timeout_step']]
                asyncio.run(monitor.check_routes())
                step_calls = [b for p, _, b in fake.requests if p.endswith('/GetCascadeTrajectorySteps')]
                self.assertEqual(len(step_calls), 1)
                self.assertEqual(step_calls[0], {'cascadeId': self.sid, 'stepOffset': 18})
                root = codex_failures.retry_notice(state, state.endpoints[('demo', 'worker')])
                self.assertIsNotNone(root)
                self.assertEqual(root['recipient'], 'parent')
                self.assertEqual(root['provider_retry']['status'], 'waiting')
                self.assertEqual(root['provider_retry']['next_at'], 115.0)
                self.assertEqual(state.get(source['id'])['state'], 'submitted')

                # Next poll while still IDLE at stepCount=19 before 15 s backoff: no second tail-step fetch.
                now[0] = 110.0
                asyncio.run(monitor.check_routes())
                step_calls = [b for p, _, b in fake.requests if p.endswith('/GetCascadeTrajectorySteps')]
                self.assertEqual(len(step_calls), 1)
                self.assertEqual(len([b for p, _, b in fake.requests if p.endswith('/SendUserCascadeMessage')]), 0)

                # At +15 s total elapsed, durable reservation precedes single empty-items wake and emits provider_retry 1/3.
                now[0] = 115.0
                asyncio.run(monitor.check_routes())
                root = codex_failures.retry_notice(state, state.endpoints[('demo', 'worker')])
                self.assertEqual(root['provider_retry']['status'], 'accepted')
                self.assertEqual(root['provider_retry']['attempts'], 1)
                events = state.action(dict(op='conversation', id=root['id']))
                retry_msgs = [json.loads(m['body']) for m in events if json.loads(m['body']).get('event') == provider_failures.RETRY_EVENT]
                self.assertEqual(len(retry_msgs), 1)
                self.assertEqual((retry_msgs[0]['attempt'], retry_msgs[0]['cap'], retry_msgs[0]['correlation']), (1, 3, 'agy:18'))
                wakes = [b for p, _, b in fake.requests if p.endswith('/SendUserCascadeMessage')]
                sends = [b for p, _, b in fake.requests if p.endswith('/SendAgentMessage')]
                self.assertEqual(len(wakes), 1)
                self.assertEqual(len(sends), 0)
                self.assertEqual(wakes[0], {
                    'cascadeId': self.sid,
                    'deliveryStrategy': 'MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION',
                })

    def test_monitor_backoff_15_30_60_cap_3_dedup_and_non_error_reset(self):
        import asyncio
        import agy_live
        import codex_failures
        import provider_failures

        state, _, _ = self._make_state()
        route = state.endpoints[('demo', 'worker')]
        now = [0.0]
        monitor = agy_live.AgyRetryMonitor(state, provider_failures.retry_policy(), clock=lambda: now[0])

        with FakeAgyServer(self.root, self.sid) as fake, \
                mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF}):
            # Stall 1 at step_index=9 (stepCount=10): requires 15 s backoff.
            fake.trajectories[self.sid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
            fake.trajectories[self.sid]['stepCount'] = 10
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 9 + [self.make_timeout_step(9)]
            asyncio.run(monitor.check_route(route))
            root = codex_failures.retry_notice(state, route)
            self.assertEqual(root['provider_retry']['next_at'], 15.0)
            now[0] = 14.0
            asyncio.run(monitor.check_route(route))
            self.assertEqual(root['provider_retry']['attempts'], 0)
            now[0] = 15.0
            asyncio.run(monitor.check_route(route))
            root = codex_failures.retry_notice(state, route)
            self.assertEqual((root['provider_retry']['status'], root['provider_retry']['attempts']), ('accepted', 1))

            # Next poll sees RUNNING (wake accepted).
            now[0] = 25.0
            asyncio.run(monitor.check_route(route))
            self.assertEqual(codex_failures.retry_notice(state, route)['provider_retry']['status'], 'accepted')

            # Stall 2 at step_index=18 (stepCount=19): requires 30 s backoff.
            fake.trajectories[self.sid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
            fake.trajectories[self.sid]['stepCount'] = 19
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 18 + [self.make_timeout_step(18)]
            now[0] = 30.0
            asyncio.run(monitor.check_route(route))
            root = codex_failures.retry_notice(state, route)
            self.assertEqual((root['provider_retry']['status'], root['provider_retry']['next_at']), ('waiting', 60.0))
            now[0] = 59.0
            asyncio.run(monitor.check_route(route))
            self.assertEqual(codex_failures.retry_notice(state, route)['provider_retry']['attempts'], 1)
            now[0] = 60.0
            asyncio.run(monitor.check_route(route))
            self.assertEqual(codex_failures.retry_notice(state, route)['provider_retry']['attempts'], 2)

            # Stall 3 at step_index=27 (stepCount=28): requires 60 s backoff.
            fake.trajectories[self.sid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
            fake.trajectories[self.sid]['stepCount'] = 28
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 27 + [self.make_timeout_step(27)]
            now[0] = 70.0
            asyncio.run(monitor.check_route(route))
            root = codex_failures.retry_notice(state, route)
            self.assertEqual((root['provider_retry']['status'], root['provider_retry']['next_at']), ('waiting', 130.0))
            now[0] = 129.0
            asyncio.run(monitor.check_route(route))
            self.assertEqual(codex_failures.retry_notice(state, route)['provider_retry']['attempts'], 2)
            now[0] = 130.0
            asyncio.run(monitor.check_route(route))
            self.assertEqual(codex_failures.retry_notice(state, route)['provider_retry']['attempts'], 3)

            # 4th stall at step_index=36 (stepCount=37): cap reached -> provider_retry_exhausted immediately, no 4th wake.
            wakes_before = len([b for p, _, b in fake.requests if p.endswith('/SendUserCascadeMessage')])
            fake.trajectories[self.sid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
            fake.trajectories[self.sid]['stepCount'] = 37
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 36 + [self.make_timeout_step(36)]
            now[0] = 140.0
            asyncio.run(monitor.check_route(route))
            root = codex_failures.retry_notice(state, route)
            self.assertEqual(root['provider_retry']['status'], 'exhausted')
            events = [json.loads(m['body']) for m in state.action(dict(op='conversation', id=root['id']))]
            retry_events = [e for e in events if e.get('event') == provider_failures.RETRY_EVENT]
            exhausted_events = [e for e in events if e.get('event') == provider_failures.EXHAUSTED]
            self.assertEqual([(e['attempt'], e['cap'], e['correlation']) for e in retry_events],
                             [(1, 3, 'agy:9'), (2, 3, 'agy:18'), (3, 3, 'agy:27')])
            self.assertEqual([(e['attempt'], e['cap'], e['correlation']) for e in exhausted_events],
                             [(3, 3, 'agy:36')])
            wakes_after = len([b for p, _, b in fake.requests if p.endswith('/SendUserCascadeMessage')])
            self.assertEqual(wakes_after, wakes_before)

            # Non-error step at step_index=37 (stepCount=38) resets the retry counter back to 0 ('recovered').
            fake.trajectories[self.sid]['stepCount'] = 38
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 38
            now[0] = 150.0
            asyncio.run(monitor.check_route(route))
            self.assertEqual(codex_failures.retry_notice(state, route)['provider_retry']['status'], 'recovered')
            self.assertEqual(codex_failures.retry_notice(state, route)['provider_retry']['attempts'], 0)

            # A subsequent stall starts a fresh episode with attempt 1 after 15 s.
            old_root_id = root['id']
            fake.trajectories[self.sid]['stepCount'] = 39
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 38 + [self.make_timeout_step(38)]
            now[0] = 160.0
            asyncio.run(monitor.check_route(route))
            new_root = codex_failures.retry_notice(state, route)
            self.assertNotEqual(new_root['id'], old_root_id)
            self.assertEqual(new_root['provider_retry']['next_at'], 175.0)
            now[0] = 175.0
            asyncio.run(monitor.check_route(route))
            self.assertEqual(codex_failures.retry_notice(state, route)['provider_retry']['attempts'], 1)

    def test_monitor_wake_acceptance_records_uncertain_and_never_wakes_twice(self):
        import asyncio
        import agy_live
        import broker
        import codex_failures
        import provider_failures
        import route_store

        state, store, _ = self._make_state()
        route = state.endpoints[('demo', 'worker')]
        now = [0.0]
        policy = provider_failures.retry_policy(backoff=(5, 10, 20), cap=3)
        monitor = agy_live.AgyRetryMonitor(state, policy=policy, clock=lambda: now[0])

        with FakeAgyServer(self.root, self.sid, wake_behavior='noop-idle') as fake, \
                mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF}):
            fake.trajectories[self.sid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
            fake.trajectories[self.sid]['stepCount'] = 10
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 9 + [self.make_timeout_step(9)]

            asyncio.run(monitor.check_route(route))
            now[0] = 5.0
            asyncio.run(monitor.check_route(route))
            root = codex_failures.retry_notice(state, route)
            self.assertEqual(root['provider_retry']['status'], 'accepted')

            # Next poll window: stepCount is still 10 and status is still IDLE -> provider_retry_uncertain and stop.
            now[0] = 15.0
            asyncio.run(monitor.check_route(route))
            root = codex_failures.retry_notice(state, route)
            self.assertEqual(root['provider_retry']['status'], 'uncertain')
            events = [json.loads(m['body']) for m in state.action(dict(op='conversation', id=root['id']))]
            uncertain = [e for e in events if e.get('event') == provider_failures.UNCERTAIN]
            self.assertEqual(len(uncertain), 1)
            self.assertIsNone(uncertain[0]['attempt'])
            self.assertEqual(uncertain[0]['correlation'], 'agy:9')

            # Even if another error step appears at stepCount=11, uncertain halt prevents waking again.
            fake.trajectories[self.sid]['stepCount'] = 11
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 10 + [self.make_timeout_step(10)]
            now[0] = 25.0
            asyncio.run(monitor.check_route(route))
            now[0] = 50.0
            asyncio.run(monitor.check_route(route))
            wakes = [b for p, _, b in fake.requests if p.endswith('/SendUserCascadeMessage')]
            self.assertEqual(len(wakes), 1)

        # Also verify broker restart during 'reserved' stops as uncertain without touching the provider.
        state2, store2, _ = self._make_state()
        route2 = state2.endpoints[('demo', 'worker')]
        monitor2 = agy_live.AgyRetryMonitor(state2, policy=policy, clock=lambda: now[0])
        with FakeAgyServer(self.root, self.sid) as fake2, \
                mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake2.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF}):
            fake2.trajectories[self.sid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
            fake2.trajectories[self.sid]['stepCount'] = 10
            fake2.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 9 + [self.make_timeout_step(9)]
            now[0] = 100.0
            asyncio.run(monitor2.check_route(route2))
            root2 = codex_failures.retry_notice(state2, route2)
            now[0] = 105.0
            monitor2.action(root2, 'reserve', fingerprint=root2['provider_retry']['fingerprint'])
        with mock.patch.object(route_store, 'verify_route', return_value=None):
            restored = broker.State(store=store2)
        restored_monitor = agy_live.AgyRetryMonitor(restored, policy=policy, clock=lambda: now[0])
        with mock.patch.object(agy_live, 'discover_servers', side_effect=AssertionError('must not poll provider')):
            asyncio.run(restored_monitor.check_route(route2))
        self.assertEqual(codex_failures.retry_notice(restored, route2)['provider_retry']['status'], 'uncertain')

    def test_monitor_notify_only_unsafe_dedup_and_no_sender_inbox_routing(self):
        import asyncio
        import agy_live
        import codex_failures
        import provider_failures

        state, _, source = self._make_state()
        route = state.endpoints[('demo', 'worker')]
        now = [0.0]
        monitor = agy_live.AgyRetryMonitor(state, provider_failures.retry_policy(), clock=lambda: now[0])

        with FakeAgyServer(self.root, self.sid) as fake, \
                mock.patch.dict(os.environ, {'AGENT_BRIDGE_AGY_URL': fake.url, 'AGENT_BRIDGE_AGY_CSRF': CSRF}):
            # 1) Notify-only terminal error notifies last sender once and does not arm or wake.
            fake.trajectories[self.sid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
            fake.trajectories[self.sid]['stepCount'] = 20
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 19 + [self.fixture['terminal_notify_only_step']]

            asyncio.run(monitor.check_route(route))
            inbox = state.action(dict(op='inbox', project='demo', name='parent'))
            self.assertEqual(len(inbox), 1)
            notice = state.action(dict(op='show', id=inbox[0]['id']))
            self.assertEqual(json.loads(notice['body'])['event'], provider_failures.DETECTED_EVENT)
            self.assertEqual(json.loads(notice['body'])['kind'], 'terminal_error')
            self.assertIsNone(codex_failures.retry_notice(state, route))

            # Same (sid, step_index, errorId) is deduplicated; a distinct errorId at the same step_index is not.
            asyncio.run(monitor.check_route(route))
            self.assertEqual(len(state.action(dict(op='inbox', project='demo', name='parent'))), 1)
            distinct_failure = dict(kind='terminal_error', failed_at='2026-11-08T12:05:00+00:00',
                                    turn_id='agy:19', error_id='00000000-0000-4000-8000-000000000999')
            second = state.action(dict(op='provider-failure', expected_route=route,
                                       source_message_id=source['id'], failure=distinct_failure))
            self.assertNotEqual(second['id'], notice['id'])

            # 2) Unsafe terminal timeout (trailing step present) -> provider_retry_unsafe, no wake.
            fake.trajectories[self.sid]['stepCount'] = 21
            fake.trajectory_steps[self.sid] = (
                [self.fixture['non_error_step']] * 20
                + [self.make_timeout_step(20), self.fixture['non_error_step']]
            )
            asyncio.run(monitor.check_route(route))
            root = codex_failures.retry_notice(state, route)
            self.assertEqual(root['provider_retry']['status'], 'unsafe')
            wakes = [b for p, _, b in fake.requests if p.endswith('/SendUserCascadeMessage')]
            self.assertEqual(len(wakes), 0)

            # 3) With no retained sender in correspondence, notice routes to the registered agy route's own inbox.
            state.messages.clear()
            state.keys.clear()
            monitor.cached_steps.clear()
            fake.trajectories[self.sid]['stepCount'] = 22
            fake.trajectory_steps[self.sid] = [self.fixture['non_error_step']] * 21 + [self.fixture['terminal_notify_only_step']]
            asyncio.run(monitor.check_route(route))
            worker_inbox = state.action(dict(op='inbox', project='demo', name='worker'))
            self.assertEqual(len(worker_inbox), 1)
            self.assertEqual(worker_inbox[0]['recipient'], 'worker')
            self.assertIsNone(json.loads(state.action(dict(op='show', id=worker_inbox[0]['id']))['body'])['source_message_id'])


if __name__ == '__main__':
    unittest.main()



