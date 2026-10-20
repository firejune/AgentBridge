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
    def __init__(self, root, sid, pid=None, protocol=1, listen=True, extra=None):
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

    def close(self):
        if self.server:
            self.server.close()


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


if __name__=='__main__':
    unittest.main()
