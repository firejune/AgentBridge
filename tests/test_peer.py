import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
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
    def __init__(self, root, sid, pid=None, protocol=1, listen=True):
        self.root, self.sid = Path(root), sid
        self.pid = os.getpid() if pid is None else pid
        self.registry = self.root/'sessions'
        self.registry.mkdir(exist_ok=True)
        self.sock = str(self.root/f'{self.pid}.sock')
        record = {'pid':self.pid,'sessionId':sid,'cwd':str(self.root),'peerProtocol':protocol,
                  'messagingSocketPath':self.sock,'name':'fake commander','status':'idle'}
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


if __name__=='__main__':
    unittest.main()
