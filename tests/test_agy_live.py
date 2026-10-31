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

    def __init__(self, root, sid, other_sid=None, send_behavior='ok'):
        self.root = Path(root).resolve()
        self.sid = sid
        self.other_sid = other_sid or str(uuid.uuid4())
        self.send_behavior = send_behavior
        self.ignore_archive = False
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

    def test_open_host_ide_creates_on_the_ide_server_with_workspace_uri(self):
        import agy_live
        with FakeAgyServer(self.root, self.sid) as fake, \
                mock.patch.object(agy_live, 'discover_servers', return_value=self.ide_servers(fake, desktop=True)), \
                mock.patch.object(agy_live, 'find_desktop_project_id') as projects:
            opened = sessions.open_session('agy', cwd=str(self.root), title='IDE Worker', host='ide')
        projects.assert_not_called()
        self.assertEqual((opened['created'], opened['host'], opened['project_id']), (True, 'ide', None))
        self.assertEqual((opened['session']['cwd'], opened['session']['name']), (str(self.root), 'IDE Worker'))
        start = [body for path, _, body in fake.requests if path.endswith('/StartCascade')]
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0]['workspaceUris'], [self.root.as_uri()])
        self.assertNotIn('projectEnvConfig', start[0])

    def test_host_ide_without_an_ide_server_fails_by_name(self):
        import agy_live
        desktop_only = [{'pid': 2001, 'app_dir': 'antigravity', 'url': 'http://127.0.0.1:1', 'csrf': CSRF, 'summaries': {}}]
        with mock.patch.object(agy_live, 'discover_servers', return_value=desktop_only):
            with self.assertRaisesRegex(ValueError, 'IDE language_server is not running'):
                agy_live.open_agy(str(self.root), host='ide')
            self.assertEqual(agy_live.discover_desktop_server()['app_dir'], 'antigravity')
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
                with mock.patch('subprocess.check_output', side_effect=fake_check_output):
                    # 1) Unmatched workspace skips orphaned pid 4000 (ppid=1) and picks first live IDE server (4001).
                    unmatched_srv = agy_live.discover_host_server('ide', cwd=str(self.root))
                    self.assertEqual(unmatched_srv['pid'], 4001)

                    # 2) Worktree wt_b resolves its .git file to repo_b and selects pid 4002.
                    wt_srv = agy_live.discover_host_server('ide', cwd=str(wt_b))
                    self.assertEqual(wt_srv['pid'], 4002)

                    # 3) Idle session prefers live server (4002) over orphaned server (4000).
                    srv_idle, row_idle = agy_live.resolve_agy(idle_sid)
                    self.assertEqual((srv_idle['pid'], row_idle['pid']), (4002, 4002))

                    # 4) Actively running session still resolves to the running server (4000).
                    srv_run, row_run = agy_live.resolve_agy(running_sid)
                    self.assertEqual((srv_run['pid'], row_run['status']), (4000, 'running'))

if __name__ == '__main__':
    unittest.main()


