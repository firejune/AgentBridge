import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
import sessions
from support import RunningBroker

TOKEN = 'ab'*16


def dead_pid():
    process = subprocess.Popen([sys.executable,'-c','pass'])
    process.wait()
    return process.pid


class FakeClaude:
    """A fake live Claude session: registry file, key file and inbox socket."""
    def __init__(self, root, sid, pid=None, protocol=1, listen=True, extra=None, ack_gate=None):
        self.root, self.sid = Path(root), sid
        self.pid = os.getpid() if pid is None else pid
        self.registry = self.root/'sessions'
        self.registry.mkdir(exist_ok=True)
        self.sock = str(self.root/f'{self.pid}.sock')
        record = {'pid':self.pid,'sessionId':sid,'cwd':str(self.root),'peerProtocol':protocol,
                  'messagingSocketPath':self.sock,'name':'fake commander','status':'idle',**(extra or {})}
        (self.registry/f'{self.pid}.json').write_text(json.dumps(record))
        digest = hashlib.sha256(os.path.abspath(self.sock).encode()).hexdigest()
        (self.registry/f'{self.pid}.{digest}.key').write_text(json.dumps({'peerToken':TOKEN}))
        self.received, self.server = [], None
        self.ack_gate = ack_gate
        if listen:
            self.server = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            self.server.bind(self.sock);self.server.listen(4)
            self.thread = threading.Thread(target=self.accept,daemon=True);self.thread.start()

    def accept(self):
        while True:
            try:
                conn,_ = self.server.accept()
            except OSError:
                return
            with conn:
                data = b''
                while chunk := conn.recv(65536):
                    data += chunk
                self.received.append([json.loads(line) for line in data.decode().splitlines()])
                if self.ack_gate is not None:
                    self.ack_gate.wait(3)

    def close(self):
        if self.server:
            self.server.close()


class DelayedBrokerReply:
    """Proxy one delivery and withhold the selected broker reply."""
    def __init__(self, root, server, phase, commit, delay=3):
        self.path = str(Path(root)/'proxy.sock')
        self.server, self.phase, self.commit = server, phase, commit
        self.delay = delay
        self.release = threading.Event()
        self.operations = []

    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen()
        self.thread = threading.Thread(target=self.relay, daemon=True)
        self.thread.start()
        return self

    def relay(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn, conn.makefile('rb') as reader:
                args = json.loads(reader.readline())
                op = args.pop('op')
                self.operations.append(op)
                if op == self.phase and not self.commit:
                    self.release.wait(self.delay)
                    return
                result = self.server.call(op, **args)
                if op == self.phase:
                    self.release.wait(self.delay)
                try:
                    conn.sendall(bridge.broker.wire({'result':result}))
                except BrokenPipeError:
                    pass
                if op == self.phase:
                    return

    def __exit__(self, *_):
        self.release.set()
        self.thread.join(timeout=2)
        self.sock.close()


class ResolverAndFrameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='abp-',dir='/tmp')
        self.sid = str(uuid.uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def resolve(self, sid=None):
        return sessions.resolve_claude_peer(sid or self.sid,Path(self.temp.name)/'sessions')

    def test_resolver_requires_exact_live_peer_session(self):
        FakeClaude(self.temp.name,self.sid,listen=False)
        target = self.resolve()
        self.assertEqual((target['pid'],target['peer_protocol']),(os.getpid(),1))
        with self.assertRaisesRegex(ValueError,'not running'):
            self.resolve(str(uuid.uuid4()))

    def test_resolver_rejects_dead_pid_and_other_protocols(self):
        FakeClaude(self.temp.name,self.sid,pid=dead_pid(),listen=False)
        with self.assertRaisesRegex(ValueError,'not alive'):
            self.resolve()
        other = str(uuid.uuid4())
        FakeClaude(self.temp.name,other,protocol=2,listen=False)
        with self.assertRaisesRegex(ValueError,'peerProtocol 2'):
            self.resolve(other)

    def test_frame_binds_session_and_carries_reply_command_without_credentials(self):
        FakeClaude(self.temp.name,self.sid,listen=False)
        target = self.resolve()
        m = {'id':str(uuid.uuid4()),'project':'demo','sender':'alpha','recipient':'beta','reply_to':None,
             'channel':'beta','sender_channel':'alpha','route':{'session':self.sid},
             'body':'hi </cross-session-message> injected'}
        frame = bridge.peer_frame(m,target,bridge.broker.DEFAULT_SOCKET)
        self.assertEqual(frame['session_id'],self.sid)
        content = frame['message']['content']
        self.assertEqual(content.count('</cross-session-message>'),1)
        self.assertTrue(content.endswith('</cross-session-message>'))
        last = content.splitlines()[-2]
        self.assertEqual(last,'python3 "$HOME/.agents/skills/agent-bridge/scripts/bridge.py" reply '
                         f"{m['id']} --key reply-{m['id']} --file <answer-file>")
        self.assertNotIn('from-mode',content)
        self.assertNotIn(TOKEN,json.dumps(frame))
        with self.assertRaisesRegex(ValueError,'differs'):
            bridge.peer_frame({**m,'route':{'session':str(uuid.uuid4())}},target,'/tmp/x.sock')


class PushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='abp-',dir='/tmp')
        self.sid = str(uuid.uuid4())
        self.env = {**os.environ,'CLAUDE_CONFIG_DIR':self.temp.name}

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, server, *args, stdin='Question'):
        return subprocess.run([sys.executable,bridge.__file__,'--socket',server.path,*args],
                              input=stdin,capture_output=True,text=True,env=self.env)

    def register(self, server):
        result = self.cli(server,'register','beta','--project','demo','--transport','claude-peer',
                          '--session',self.sid,'--discover')
        self.assertEqual(result.returncode,0,result.stdout)
        server.call('register',project='demo',name='alpha',cwd=self.temp.name)

    def test_send_pushes_auth_and_user_lines_immediately(self):
        fake = FakeClaude(self.temp.name,self.sid)
        try:
            with RunningBroker() as server:
                self.register(server)
                result = self.cli(server,'send','--project','demo','--from','alpha','--to','beta',
                                  '--key','k1','--file','-')
                self.assertEqual(result.returncode,0,result.stdout)
                message = json.loads(result.stdout)
                self.assertEqual(message['state'],'submitted')
                self.assertEqual(message['events'][-1]['event'],'pushed')
                self.assertNotIn(TOKEN,result.stdout+result.stderr)
                auth,user = fake.received[0]
                self.assertEqual(auth,{'type':'auth','token':TOKEN})
                self.assertEqual((user['type'],user['session_id']),('user',self.sid))
                self.assertIn(message['id'],user['message']['content'])
                # Same key again: already submitted, so no second push.
                again = self.cli(server,'send','--project','demo','--from','alpha','--to','beta',
                                 '--key','k1','--file','-')
                self.assertEqual(again.returncode,0)
                self.assertEqual(len(fake.received),1)
        finally:
            fake.close()

    def test_push_failure_keeps_message_pending_and_exits_nonzero(self):
        FakeClaude(self.temp.name,self.sid,listen=False)  # registry present, inbox socket absent
        with RunningBroker() as server:
            self.register(server)
            result = self.cli(server,'send','--project','demo','--from','alpha','--to','beta',
                              '--key','k1','--file','-')
            self.assertEqual(result.returncode,1)
            out = json.loads(result.stdout)
            self.assertTrue(out['push_failed'])
            self.assertIn('push_failed: Claude messaging socket',out['error'])
            stored = server.call('show',id=out['message']['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual(stored['events'][-1]['event'],'push_failed')
            self.assertNotIn(TOKEN,result.stdout+result.stderr)

    def post(self, server, key, recipient='beta'):
        return server.call('send', project='demo', sender='alpha', recipient=recipient,
                           body='private body ' + key, key=key)

    def deliver(self, path, mid, timeout):
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch('sys.stdout', stdout):
            started = time.monotonic()
            code = bridge.main(['--socket', path, 'deliver', mid, '--timeout', str(timeout)])
            elapsed = time.monotonic() - started
        return code, json.loads(stdout.getvalue()), elapsed

    def test_deliver_records_uncertain_after_peer_eof_timeout_and_delayed_finish(self):
        release = threading.Event()
        fake = FakeClaude(self.temp.name, self.sid, ack_gate=release)
        try:
            with RunningBroker() as server:
                self.register(server)
                first = self.post(server, 'first')
                with DelayedBrokerReply(self.temp.name, server, 'finish', True, delay=0.25) as proxy:
                    code, output, elapsed = self.deliver(proxy.path, first['id'], 0.4)
                self.assertEqual(code, 1)
                self.assertGreater(elapsed, 0.55)
                self.assertLess(elapsed, 1.5)
                self.assertIn('did not close', output['error'])
                self.assertEqual(proxy.operations, ['show', 'claim', 'finish'])
                self.assertEqual(output['message']['state'], 'uncertain')
                self.assertEqual(len(fake.received), 1)
                stored = server.call('show', id=first['id'])
                self.assertEqual([e['state'] for e in stored['events']], ['dispatching', 'uncertain'])
                result = self.cli(server, 'send', '--project', 'demo', '--from', 'alpha',
                                  '--to', 'beta', '--key', 'second', '--file', '-')
                self.assertEqual(result.returncode, 1)
                row, = (row for row in server.call('inbox', project='demo', name='beta')
                        if row['state'] == 'pending')
                self.assertIn(row['id'], result.stdout)
                self.assertIn(f"pending behind {first['id']} since {stored['dispatch_started_at']}", result.stdout)
                self.assertNotIn(first['body'], result.stdout)
                self.assertEqual(len(fake.received), 1)
        finally:
            release.set()
            fake.close()

    def test_delayed_claim_or_finish_reply_respects_deliver_budget_and_never_replays(self):
        fake = FakeClaude(self.temp.name, self.sid)
        try:
            with RunningBroker() as server:
                self.register(server)
                cases = (('claim', True, 'dispatching', 0), ('finish', True, 'submitted', 1),
                         ('finish', False, 'dispatching', 2))
                for phase, commit, state, count in cases:
                    with self.subTest(phase=phase, commit=commit):
                        message = self.post(server, f'{phase}-{commit}')
                        with mock.patch.object(bridge, 'PEER_BROKER_TIMEOUT', 0.45), \
                                DelayedBrokerReply(self.temp.name, server, phase, commit) as proxy:
                            code, output, elapsed = self.deliver(proxy.path, message['id'], 0.35)
                        Path(proxy.path).unlink()
                        self.assertEqual(code, 1)
                        self.assertLess(elapsed, 0.9)
                        if phase == 'finish':
                            self.assertGreaterEqual(elapsed, 0.45)
                        self.assertEqual((output['stage'], output['id'], output['uncertain']),
                                         (phase, message['id'], True))
                        self.assertNotIn(message['body'], json.dumps(output))
                        self.assertEqual(proxy.operations,
                                         ['show', 'claim'] if phase == 'claim' else ['show', 'claim', 'finish'])
                        self.assertEqual(server.call('show', id=message['id'])['state'], state)
                        self.assertEqual(len(fake.received), count)
                        if phase == 'claim':
                            server.call('resolve', id=message['id'], state='done', note='Fake peer saw no write')
                        if not commit:
                            later = self.post(server, 'later')
                            self.assertEqual(server.call('show', id=later['id'])['blocked_by']['id'], message['id'])
        finally:
            fake.close()

    def test_dispatch_blocker_visible_in_inbox_across_aliases_and_restart(self):
        FakeClaude(self.temp.name, self.sid, listen=False)
        with RunningBroker() as server:
            self.register(server)
            server.call('register', project='demo', name='alias', cwd=self.temp.name,
                        transport='claude-peer', session=self.sid)
            first = self.post(server, 'first')
            claimed = server.call('claim', id=first['id'])
            second = self.post(server, 'second', recipient='alias')
            since = claimed['events'][-1]['at']
            for state in ('dispatching', 'uncertain'):
                with self.subTest(state=state):
                    row, = server.call('inbox', project='demo', name='alias')
                    shown = server.call('show', id=second['id'])
                    blocker = {'id':first['id'], 'state':state, 'since':since}
                    self.assertEqual(row['blocked_by'], blocker)
                    self.assertEqual(shown['blocked_by'], blocker)
                    self.assertEqual(row['note'], f"pending behind {first['id']} since {since}")
                    self.assertNotIn(first['body'], json.dumps(row))
                    self.assertEqual(shown['state'], 'pending')
                    if state == 'dispatching':
                        server.restart()
            server.call('resolve', id=first['id'], state='done', note='Test operator released the guard')
            row, = server.call('inbox', project='demo', name='alias')
            self.assertNotIn('blocked_by', row)
            self.assertIsNone(server.call('show', id=second['id'])['note'])

    def test_late_finish_cannot_settle_a_new_claim_after_manual_resolution(self):
        FakeClaude(self.temp.name, self.sid, listen=False)
        with RunningBroker() as server:
            self.register(server)
            message = self.post(server, 'first')
            mid = message['id']
            first = server.call('claim', id=mid, guarded=True)['dispatch_claim']
            server.call('resolve', id=mid, state='pending', note='Test operator verified no write')
            second = server.call('claim', id=mid, guarded=True)['dispatch_claim']
            self.assertNotEqual(first, second)
            with self.assertRaisesRegex(ValueError, 'Delivery claim changed'):
                server.call('finish', id=mid, state='submitted', expect_dispatch_claim=first)
            self.assertEqual(server.call('show', id=mid)['state'], 'dispatching')
            self.assertEqual(server.call('finish', id=mid, state='submitted',
                                         expect_dispatch_claim=second)['state'], 'submitted')

    def test_legacy_finish_is_refused_against_a_guarded_reclaim(self):
        FakeClaude(self.temp.name, self.sid, listen=False)
        with RunningBroker() as server:
            self.register(server)
            mid = self.post(server, 'mixed-claim')['id']
            self.assertNotIn('dispatch_claim', server.call('claim', id=mid))
            server.call('resolve', id=mid, state='pending', note='Test operator released the old claim')
            claimed = server.call('claim', id=mid, guarded=True)
            self.assertEqual(set(claimed), {'dispatch_claim'})
            with self.assertRaisesRegex(ValueError, 'Delivery claim changed'):
                server.call('finish', id=mid, state='submitted')
            self.assertEqual(server.call('show', id=mid)['state'], 'dispatching')
            self.assertEqual(server.call('finish', id=mid, state='submitted',
                                         expect_dispatch_claim=claimed['dispatch_claim'])['state'], 'submitted')
            legacy = self.post(server, 'legacy-claim')['id']
            self.assertNotIn('dispatch_claim', server.call('claim', id=legacy))
            self.assertEqual(server.call('finish', id=legacy, state='submitted')['state'], 'submitted')

    def test_guarded_finish_after_release_or_reply_uses_current_state(self):
        FakeClaude(self.temp.name, self.sid, listen=False)
        with RunningBroker() as server:
            self.register(server)
            for state in ('pending', 'answered', 'uncertain'):
                with self.subTest(state=state):
                    mid = self.post(server, state)['id']
                    claim = server.call('claim', id=mid, guarded=True)['dispatch_claim']
                    if state == 'pending':
                        server.call('resolve', id=mid, state=state, note='Test operator released the claim')
                    elif state == 'uncertain':
                        server.restart()
                    else:
                        server.call('reply', id=mid, key='fast-reply', body='Fast reply')
                    if state == 'answered':
                        self.assertEqual(server.call('finish', id=mid, state='submitted',
                                                     expect_dispatch_claim=claim)['state'], state)
                    else:
                        with self.assertRaisesRegex(ValueError, 'Delivery claim changed'):
                            server.call('finish', id=mid, state='submitted', expect_dispatch_claim=claim)
                        self.assertEqual(server.call('show', id=mid)['state'], state)
                    if state == 'uncertain':
                        server.call('resolve', id=mid, state='done', note='Release the restart test blocker')

    def test_errors_from_sendall_and_shutdown_are_potential_writes(self):
        target = {'socket':'/fake/peer.sock'}
        for method, error, expected in (('sendall', TimeoutError(), 'write timed out'),
                                        ('shutdown', OSError('lost'), 'error during the write')):
            with self.subTest(method=method), mock.patch.object(bridge, 'peer_token', return_value=TOKEN), \
                    mock.patch.object(socket, 'socket') as connection:
                sock = connection.return_value.__enter__.return_value
                getattr(sock, method).side_effect = error
                self.assertIn(expected, bridge.push_frame(target, {}, time.monotonic() + 1))
                sock.sendall.assert_called_once()
                sock.recv.assert_not_called()

    def test_socket_teardown_error_after_eof_is_a_potential_write(self):
        target = {'socket':'/fake/peer.sock'}
        with mock.patch.object(bridge, 'peer_token', return_value=TOKEN), \
                mock.patch.object(socket, 'socket') as connection:
            sock = connection.return_value.__enter__.return_value
            sock.recv.return_value = b''
            connection.return_value.__exit__.side_effect = OSError('teardown failed')
            self.assertIn('error after the write', bridge.push_frame(target, {}, time.monotonic() + 1))
            sock.sendall.assert_called_once()
            sock.recv.assert_called_once()

    def test_dead_session_fails_loudly_on_ask(self):
        fake = FakeClaude(self.temp.name,self.sid)
        try:
            with RunningBroker() as server:
                self.register(server)
                (Path(self.temp.name)/'sessions'/f'{os.getpid()}.json').unlink()
                result = self.cli(server,'ask','--project','demo','--from','alpha','--to','beta',
                                  '--key','k1','--file','-','--timeout','1')
                self.assertEqual(result.returncode,1)
                self.assertIn('not running',json.loads(result.stdout)['error'])
                self.assertEqual(fake.received,[])
        finally:
            fake.close()


class OpenClaudeTests(unittest.TestCase):
    """register --transport claude-peer --new: open the composer link, bind the new live record."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='abn-',dir='/tmp')
        self.root = Path(self.temp.name)
        self.sid = str(uuid.uuid4())
        self.fakes, self.urls = [], []

    def tearDown(self):
        for fake in self.fakes:
            fake.close()
        self.temp.cleanup()

    DESKTOP = {'entrypoint':'claude-desktop','hostSessionId':'local_fake'}

    def opener(self, sid=None, extra=DESKTOP):
        def open_link(url):
            self.urls.append(url)
            self.fakes.append(FakeClaude(self.temp.name,sid or self.sid,extra=extra))
        return open_link

    def open(self, **kwargs):
        kwargs.setdefault('opener',self.opener())
        return sessions.open_claude(str(self.root),sessions_dir=self.root/'sessions',poll=0.01,**kwargs)

    def test_link_opens_composer_at_folder_and_binds_new_record(self):
        opened = self.open(title='Fresh worker')
        self.assertTrue(opened['created'])
        self.assertEqual(opened['session']['id'],self.sid)
        self.assertEqual(opened['desktop_session'],'local_fake')
        self.assertEqual(opened['session']['cwd'],str(self.root))
        url = self.urls[0]
        self.assertTrue(url.startswith('claude://code/new?'),url)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['folder'],[str(self.root.resolve())])
        self.assertEqual(query['q'],['Fresh worker'])

    def test_trust_probe_without_desktop_host_is_not_taken(self):
        probe = {'entrypoint':'claude-desktop'}  # the app's temporary query process
        with self.assertRaisesRegex(ValueError,'No new live Claude Code session'):
            self.open(opener=self.opener(extra=probe),timeout=0.05)

    def test_preexisting_session_in_same_folder_is_not_taken(self):
        self.fakes.append(FakeClaude(self.temp.name,str(uuid.uuid4()),extra=self.DESKTOP))
        with self.assertRaisesRegex(ValueError,'No new live Claude Code session'):
            self.open(opener=self.urls.append,timeout=0.05)
        self.assertEqual(len(self.urls),1)

    def test_rejects_model_missing_folder_and_bad_timeout_before_opening(self):
        with self.assertRaisesRegex(ValueError,'carries no model'):
            self.open(model='opus')
        with self.assertRaisesRegex(ValueError,'does not exist'):
            sessions.open_claude(str(self.root/'missing'),opener=self.urls.append)
        with self.assertRaisesRegex(ValueError,'Timeout'):
            self.open(timeout=0)
        self.assertEqual(self.urls,[])

    def test_register_new_parsing_rejects_conflicts(self):
        for extra in (['--session',self.sid],['--discover']):
            with self.assertRaisesRegex(ValueError,'cannot be combined'):
                bridge.registration(self.args(*extra))
        with self.assertRaisesRegex(ValueError,'supported only with'):
            bridge.registration(self.args(transport='realtime'))
        args = self.args()
        args.cwd = None
        with self.assertRaisesRegex(ValueError,'requires --cwd'):
            bridge.registration(args)

    def args(self, *extra, transport='claude-peer'):
        import argparse
        ns = argparse.Namespace(new=True,discover='--discover' in extra,
                                session=extra[1] if extra[:1]==('--session',) else None,
                                transport=transport,cwd=str(self.root),title=None,model=None,timeout=1)
        return ns

    def test_register_new_binds_route_and_send_lands(self):
        out = io.StringIO()
        with RunningBroker() as server, mock.patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':self.temp.name}), \
                mock.patch.object(sessions,'open_url',self.opener()), mock.patch('sys.stdout',out):
            code = bridge.main(['--socket',server.path,'register','fresh','--project','demo',
                                '--transport','claude-peer','--new','--cwd',str(self.root),'--timeout','5'])
            self.assertEqual(code,0,out.getvalue())
            reg = json.loads(out.getvalue().splitlines()[-1])
            self.assertEqual((reg['session'],reg['transport']),(self.sid,'claude-peer'))
            self.assertTrue(reg['discovery']['created'])
            server.call('register',project='demo',name='alpha',cwd=self.temp.name)
            with mock.patch('sys.stdin',io.StringIO('hello')):
                code = bridge.main(['--socket',server.path,'send','--project','demo','--from','alpha',
                                    '--to','fresh','--key','k1','--file','-'])
            self.assertEqual(code,0,out.getvalue())
            self.assertEqual(json.loads(out.getvalue().splitlines()[-1])['state'],'submitted')
            _,user = self.fakes[0].received[0]
            self.assertEqual(user['session_id'],self.sid)
            self.assertIn('hello',user['message']['content'])


class OpenClaudeIdeTests(unittest.TestCase):
    """open/register --new --host ide: focus the folder's window, open the extension link, bind the new record."""
    IDE = {'entrypoint':'claude-vscode'}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='abi-',dir='/tmp')
        self.root = Path(self.temp.name)
        self.sid = str(uuid.uuid4())
        self.fakes, self.urls, self.focused = [], [], []

    def tearDown(self):
        for fake in self.fakes:
            fake.close()
        self.temp.cleanup()

    def opener(self, *records):
        def open_link(url):
            self.urls.append(url)
            for sid, pid, extra in records:
                self.fakes.append(FakeClaude(self.temp.name,sid,pid=pid,extra=extra))
        return open_link

    def open(self, **kwargs):
        kwargs.setdefault('opener',self.opener((self.sid,None,self.IDE)))
        kwargs.setdefault('focuser',lambda ide,folder: self.focused.append((ide,folder)))
        return sessions.open_claude_ide(str(self.root),sessions_dir=self.root/'sessions',settle=0,poll=0.01,**kwargs)

    def test_focuses_folder_then_binds_new_ide_record_without_prefill(self):
        opened = self.open()
        self.assertEqual((opened['created'],opened['host'],opened['ide']),(True,'ide','antigravity-ide'))
        self.assertEqual(opened['session']['id'],self.sid)
        self.assertEqual(self.focused,[('antigravity-ide',self.root.resolve())])
        self.assertEqual(self.urls,['antigravity-ide://anthropic.claude-code/open'])
        self.assertIsNone(opened['prompt'])

    def test_title_is_not_prefilled_in_ide_and_ide_picks_scheme(self):
        # Issue #87: nothing sends an IDE composer prefill, so it would sit unsent.
        opened = self.open(title='Fresh worker',ide='cursor')
        parts = urlsplit(self.urls[0])
        self.assertEqual((parts.scheme,parts.netloc,parts.path),('cursor','anthropic.claude-code','/open'))
        self.assertNotIn('prompt',parse_qs(parts.query))
        self.assertIsNone(opened['prompt'])
        self.assertIn('ignored on --host ide',opened['warning'])

    def test_desktop_record_and_preexisting_ide_record_are_not_taken(self):
        self.fakes.append(FakeClaude(self.temp.name,str(uuid.uuid4()),pid=os.getppid(),extra=self.IDE))
        with self.assertRaisesRegex(ValueError,'No new live Claude IDE session'):
            self.open(opener=self.urls.append,timeout=0.05)
        with self.assertRaisesRegex(ValueError,'No new live Claude IDE session'):
            self.open(opener=self.opener((self.sid,None,{'entrypoint':'claude-desktop','hostSessionId':'local_x'})),timeout=0.05)

    def test_record_in_another_window_is_reported_as_stray(self):
        other = tempfile.mkdtemp(prefix='abo-',dir='/tmp')
        try:
            with self.assertRaisesRegex(ValueError,'reached another window') as caught:
                self.open(opener=self.opener((self.sid,None,{**self.IDE,'cwd':other})),timeout=1)
            self.assertIn(self.sid,str(caught.exception))
        finally:
            os.rmdir(other)

    def test_two_new_records_fail_without_choosing(self):
        two = self.opener((self.sid,None,self.IDE),(str(uuid.uuid4()),os.getppid(),self.IDE))
        with self.assertRaisesRegex(ValueError,'not choosing one'):
            self.open(opener=two,timeout=1)

    def test_rejects_model_unknown_ide_and_missing_folder_before_focusing(self):
        with self.assertRaisesRegex(ValueError,'carries no model'):
            self.open(model='opus')
        with self.assertRaisesRegex(ValueError,'Unknown IDE'):
            self.open(ide='notepad')
        with self.assertRaisesRegex(ValueError,'does not exist'):
            sessions.open_claude_ide(str(self.root/'missing'),opener=self.urls.append,focuser=self.focused.append)
        self.assertEqual((self.urls,self.focused),([],[]))

    def test_open_session_routes_hosts(self):
        with mock.patch.object(sessions,'open_claude_ide',return_value='ide') as ide, \
                mock.patch.object(sessions,'open_claude',return_value='desktop') as desk:
            self.assertEqual(sessions.open_session('claude',cwd=str(self.root),host='ide'),'ide')
            self.assertEqual(sessions.open_session('claude',cwd=str(self.root)),'desktop')
        self.assertEqual(ide.call_args.kwargs['ide'],'antigravity-ide')
        desk.assert_called_once()
        with self.assertRaisesRegex(ValueError,'applies only to Claude'):
            sessions.open_session('codex',cwd=str(self.root),host='ide')
        with self.assertRaisesRegex(ValueError,'needs --provider claude --host ide'):
            sessions.open_session('claude',cwd=str(self.root),ide='vscode')

    def test_register_new_passes_host_and_ide(self):
        import argparse
        args = argparse.Namespace(new=True,discover=False,session=None,transport='claude-peer',cwd=str(self.root),
                                  title=None,model=None,timeout=1,host='ide',ide='vscode')
        row = {'session':{'id':self.sid,'cwd':str(self.root)}}
        with mock.patch.object(sessions,'open_session',return_value=row) as opened:
            bridge.registration(args)
        self.assertEqual((opened.call_args.kwargs['host'],opened.call_args.kwargs['ide']),('ide','vscode'))
        self.assertEqual(args.session,self.sid)


if __name__=='__main__':
    unittest.main()
