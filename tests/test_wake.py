"""Issue #115: wake an idle-closed Claude Code session, then push once."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
import sessions
from support import RunningBroker
from test_peer import FakeClaude

HOST = 'local_11111111-2222-3333-4444-555555555555'
DESKTOP = {'entrypoint':'claude-desktop','hostSessionId':HOST}


class WakeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='abw-',dir='/tmp')
        self.root = Path(self.temp.name)
        self.sid = str(uuid.uuid4())
        self.desktop = self.root/'desktop'/'acct'/'org'
        self.desktop.mkdir(parents=True)
        self.fakes, self.urls = [], []

    def tearDown(self):
        for fake in self.fakes:
            fake.close()
        self.temp.cleanup()

    def metadata(self, host=HOST, sid=None, **extra):
        data = {'sessionId':host,'cliSessionId':sid or self.sid,'cwd':str(self.root),'isArchived':False,**extra}
        (self.desktop/f'{host}.json').write_text(json.dumps(data,indent=2))

    def opener(self, appear=True, socket=True, extra=DESKTOP):
        """What Claude Desktop does with the link: respawn the same session under a new pid."""
        def open_link(url):
            self.urls.append(url)
            if not appear:
                return
            # The parent process stands in for the app's new CLI: alive, and a different pid.
            fake = FakeClaude(self.temp.name,self.sid,pid=os.getppid(),listen=socket,extra=extra)
            if not socket:
                record = fake.registry/f'{fake.pid}.json'
                data = json.loads(record.read_text())
                data.pop('messagingSocketPath')
                record.write_text(json.dumps(data))
            self.fakes.append(fake)
        return open_link

    def wake(self, **kwargs):
        kwargs.setdefault('opener',self.opener())
        kwargs.setdefault('timeout',2)
        return sessions.wake_claude(self.sid,sessions_dir=self.root/'sessions',desktop_dir=self.root/'desktop',
                                    projects_dir=self.root/'projects',poll=0.01,**kwargs)


class WakeClaudeTests(WakeFixture):
    def test_live_session_is_already_live_and_opens_nothing(self):
        self.fakes.append(FakeClaude(self.temp.name,self.sid,extra=DESKTOP))
        self.metadata()
        found = self.wake()
        self.assertEqual(found['state'],'already-live')
        self.assertEqual(self.urls,[])

    def test_closed_desktop_session_is_woken_by_its_link(self):
        self.metadata()
        found = self.wake()
        self.assertEqual(found['state'],'woken')
        self.assertEqual(self.urls,[f'claude://claude.ai/epitaxy/{HOST}'])
        self.assertEqual((found['desktop_session'],found['peer']['id']),(HOST,self.sid))
        self.assertEqual(found['peer']['pid'],os.getppid())

    def test_dead_record_left_behind_still_wakes(self):
        dead = subprocess.Popen([sys.executable,'-c','pass']);dead.wait()
        FakeClaude(self.temp.name,self.sid,pid=dead.pid,listen=False,extra=DESKTOP)
        self.metadata()
        self.assertEqual(self.wake()['state'],'woken')

    def test_link_with_no_record_is_uncertain_and_opened_once(self):
        self.metadata()
        found = self.wake(opener=self.opener(appear=False),timeout=0.2)
        self.assertEqual(found['state'],'uncertain')
        self.assertIn('no live record',found['reason'])
        self.assertEqual(len(self.urls),1)

    def test_record_without_socket_is_uncertain(self):
        self.metadata()
        found = self.wake(opener=self.opener(socket=False),timeout=0.2)
        self.assertEqual(found['state'],'uncertain')
        self.assertIn('no messaging socket',found['reason'])
        self.assertEqual(len(self.urls),1)

    def test_record_under_another_desktop_session_is_uncertain(self):
        self.metadata()
        other = {'entrypoint':'claude-desktop','hostSessionId':'local_99999999-0000-0000-0000-000000000000'}
        found = self.wake(opener=self.opener(extra=other))
        self.assertEqual(found['state'],'uncertain')
        self.assertNotIn('peer',found)

    def test_scheduled_task_session_is_unsupported_without_opening(self):
        self.metadata(scheduledTaskId='nightly-index')
        found = self.wake()
        self.assertEqual(found['state'],'unsupported')
        self.assertIn('scheduled task',found['reason'])
        self.assertEqual(self.urls,[])

    def test_archived_session_is_unsupported_without_opening(self):
        self.metadata(isArchived=True)
        self.assertEqual(self.wake()['state'],'unsupported')
        self.assertEqual(self.urls,[])

    def test_running_session_with_inbox_off_is_unsupported(self):
        fake = FakeClaude(self.temp.name,self.sid,listen=False,extra=DESKTOP)
        record = fake.registry/f'{fake.pid}.json'
        data = json.loads(record.read_text());data.pop('messagingSocketPath');record.write_text(json.dumps(data))
        self.metadata()
        found = self.wake()
        self.assertEqual(found['state'],'unsupported')
        self.assertIn('inbox off',found['reason'])
        self.assertEqual(self.urls,[])

    def test_ide_session_is_unsupported_with_reason(self):
        transcript = self.root/'projects'/'-ws'/f'{self.sid}.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'type':'user','sessionId':self.sid,'entrypoint':'claude-vscode'})+'\n')
        found = self.wake()
        self.assertEqual((found['state'],found['host']),('unsupported','claude-vscode'))
        self.assertIn('IDE',found['reason'])
        self.assertEqual(self.urls,[])

    def test_unknown_host_is_unsupported(self):
        found = self.wake()
        self.assertEqual(found['state'],'unsupported')
        self.assertEqual(self.urls,[])

    def test_two_desktop_sessions_for_one_cli_session_are_refused(self):
        self.metadata()
        self.metadata(host='local_99999999-0000-0000-0000-000000000000')
        with self.assertRaises(sessions.DiscoveryError):
            self.wake()
        self.assertEqual(self.urls,[])

    def test_symlinked_copies_of_one_metadata_file_count_once(self):
        self.metadata()
        (self.root/'desktop'/'alias').symlink_to(self.root/'desktop'/'acct')
        self.assertEqual(self.wake()['state'],'woken')


class WakePushTests(WakeFixture):
    """pending -> wake -> delivered once; an uncertain wake never sends."""
    def send(self, server, key='k1'):
        server.call('register',project='demo',name='alpha',cwd=self.temp.name)
        server.call('register',project='demo',name='beta',cwd=self.temp.name,transport='claude-peer',session=self.sid)
        return server.call('send',project='demo',sender='alpha',recipient='beta',body='Question',key=key)

    def env(self):
        return mock.patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':self.temp.name,
                                           'CLAUDE_DESKTOP_SESSIONS_DIR':str(self.root/'desktop')})

    def test_pending_message_is_pushed_once_after_wake(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()), \
                mock.patch.object(bridge,'TITLE_WAIT',0.0):
            message = self.send(server)
            self.assertEqual(message['state'],'pending')
            result = bridge.push_if_peer(server.path,message)
            self.assertEqual(result['state'],'submitted')
            self.assertEqual(result['wake']['state'],'woken')
            self.assertNotIn('peer',result['wake'])
            self.assertIn('after wake (woken)',result['events'][-1]['note'])
            self.assertEqual(len(self.urls),1)
            self.assertEqual(len(self.fakes[0].received),1)
            auth,user = self.fakes[0].received[0]
            self.assertEqual(user['session_id'],self.sid)
            self.assertIn(message['id'],user['message']['content'])
            # Not pending any more: nothing pushes it a second time.
            with self.assertRaises(ValueError):
                bridge.deliver(server.path,message['id'])
            self.assertEqual(len(self.fakes[0].received),1)
            self.assertEqual(len(self.urls),1)

    def test_uncertain_wake_returns_message_to_pending_and_sends_nothing(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener(socket=False)), \
                mock.patch.object(bridge,'push_frame') as frame:
            message = self.send(server)
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path,message,wake_timeout=0.2)
            self.assertIn('wake uncertain',str(caught.exception))
            self.assertEqual(caught.exception.message['wake']['state'],'uncertain')
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual(stored['events'][-1]['event'],'push_failed')
            self.assertEqual(len(self.urls),1)
            frame.assert_not_called()

    def test_unsupported_wake_keeps_the_message_pending_without_opening(self):
        self.metadata(scheduledTaskId='nightly-index')
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()):
            message = self.send(server)
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push_if_peer(server.path,message)
            self.assertIn('wake unsupported',str(caught.exception))
            self.assertEqual(server.call('show',id=message['id'])['state'],'pending')
            self.assertEqual(self.urls,[])

    def test_wake_stays_inside_the_delivery_budget(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()), \
                mock.patch.object(sessions,'wake_claude',wraps=sessions.wake_claude) as wake:
            message = self.send(server)
            # Less than PEER_IO_TIMEOUT left: nothing is opened and nothing is sent.
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path,message,deadline=time.monotonic()+bridge.PEER_IO_TIMEOUT-1)
            self.assertIn('no time to wake',str(caught.exception))
            self.assertEqual(server.call('show',id=message['id'])['state'],'pending')
            self.assertEqual(self.urls,[])
            wake.assert_not_called()
            # A short budget shrinks the wait below wake_timeout, keeping PEER_IO_TIMEOUT for the push.
            with mock.patch.object(bridge,'TITLE_WAIT',0.0):
                result = bridge.push(server.path,message,deadline=time.monotonic()+bridge.PEER_IO_TIMEOUT+3)
            self.assertEqual(result['state'],'submitted')
            self.assertLessEqual(wake.call_args.kwargs['timeout'],3)

    def test_live_session_push_does_not_wake(self):
        self.fakes.append(FakeClaude(self.temp.name,self.sid,extra=DESKTOP))
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()), \
                mock.patch.object(bridge,'TITLE_WAIT',0.0):
            result = bridge.push_if_peer(server.path,self.send(server))
            self.assertEqual(result['state'],'submitted')
            self.assertNotIn('wake',result)
            self.assertEqual(self.urls,[])


class WakeCommandTests(WakeFixture):
    def cli(self, server, *args):
        env = {**os.environ,'CLAUDE_CONFIG_DIR':self.temp.name,'CLAUDE_DESKTOP_SESSIONS_DIR':str(self.root/'desktop')}
        return subprocess.run([sys.executable,bridge.__file__,'--socket',server.path,*args],
                              capture_output=True,text=True,env=env)

    def test_live_route_is_already_live(self):
        self.fakes.append(FakeClaude(self.temp.name,self.sid,extra=DESKTOP))
        with RunningBroker() as server:
            server.call('register',project='demo',name='beta',cwd=self.temp.name,transport='claude-peer',session=self.sid)
            result = self.cli(server,'wake','beta','--project','demo')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            out = json.loads(result.stdout)
            self.assertEqual((out['state'],out['name']),('already-live','beta'))
            self.assertNotIn('peer',out)

    def test_other_transports_are_unsupported(self):
        with RunningBroker() as server:
            server.call('register',project='demo',name='naru',cwd=self.temp.name,transport='codex',
                        session=str(uuid.uuid4()))
            result = self.cli(server,'wake','naru','--project','demo')
            self.assertEqual(result.returncode,1)
            out = json.loads(result.stdout)
            self.assertEqual(out['state'],'unsupported')
            self.assertIn('remap',out['reason'])

    def test_closed_route_without_desktop_session_is_unsupported(self):
        with RunningBroker() as server:
            server.call('register',project='demo',name='beta',cwd=self.temp.name,transport='claude-peer',session=self.sid)
            result = self.cli(server,'wake','beta','--project','demo','--timeout','1')
            self.assertEqual(result.returncode,1)
            self.assertEqual(json.loads(result.stdout)['state'],'unsupported')


if __name__ == '__main__':
    unittest.main()
