"""Issue #115: wake an idle-closed Claude Code session, then push once."""
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
        def open_link(url, timeout=None):
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

    def test_opener_timeout_returns_message_to_pending_without_blocking(self):
        self.metadata()
        def hang(url, timeout=None):
            self.urls.append(url)
            raise subprocess.TimeoutExpired(['open','-g',url],10)
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',hang), \
                mock.patch.object(bridge,'push_frame') as frame:
            message = self.send(server)
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path,message,wake_timeout=2)
            self.assertEqual(caught.exception.message['wake']['state'],'uncertain')
            self.assertIn('TimeoutExpired',str(caught.exception))
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual(stored['events'][-1]['event'],'push_failed')
            self.assertNotIn('uncertain',[e['state'] for e in stored['events']])
            # The recipient is not blocked: another message to it is claimable at once.
            later = self.send(server,key='k2')
            self.assertNotIn('blocked_by',server.call('show',id=later['id']))
            self.assertEqual(len(self.urls),1)
            frame.assert_not_called()

    def test_blocking_opener_stays_inside_a_short_deliver_deadline(self):
        # Real time: the opener blocks in a child process until its own subprocess timeout fires.
        self.metadata()
        given = []
        def blocking(url, timeout=sessions.WAKE_OPEN_TIMEOUT):
            given.append(timeout)
            subprocess.run([sys.executable,'-c','import time; time.sleep(30)'],timeout=timeout)
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',blocking), \
                mock.patch.object(bridge,'push_frame') as frame:
            message = self.send(server)
            started = time.monotonic()
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.deliver(server.path,message['id'],timeout=6)
            elapsed = time.monotonic()-started
            self.assertLess(elapsed,6.5)
            self.assertEqual(len(given),1)
            self.assertLessEqual(given[0],6-bridge.PEER_IO_TIMEOUT)
            self.assertEqual(caught.exception.message['wake']['state'],'uncertain')
            stored = server.call('show',id=message['id'])
            self.assertEqual((stored['state'],stored['events'][-1]['event']),('pending','push_failed'))
            self.assertNotIn('uncertain',[e['state'] for e in stored['events']])
            frame.assert_not_called()

    def test_interrupt_before_the_write_is_pending_and_during_it_uncertain(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',side_effect=KeyboardInterrupt):
            message = self.send(server)
            with self.assertRaises(KeyboardInterrupt):
                bridge.push(server.path,message,wake_timeout=2)
            stored = server.call('show',id=message['id'])
            self.assertEqual((stored['state'],stored['events'][-1]['event']),('pending','push_failed'))
        self.fakes.append(FakeClaude(self.temp.name,self.sid,extra=DESKTOP))
        with RunningBroker() as server, self.env(), \
                mock.patch.object(bridge,'push_frame',side_effect=KeyboardInterrupt):
            message = self.send(server)
            with self.assertRaises(KeyboardInterrupt):
                bridge.push(server.path,message,wake_timeout=2)
            self.assertEqual(server.call('show',id=message['id'])['state'],'uncertain')

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


class WaitBehindWakeTests(WakeFixture):
    """A second message to the same recipient during a wake waits behind it, never silently pending."""
    def setUp(self):
        super().setUp()
        self.opening = threading.Event()

    def slow(self, **kwargs):
        inner = self.opener(**kwargs)
        def open_link(url, timeout=None):
            self.opening.set()  # The first message is claimed and waking now.
            time.sleep(0.5)
            inner(url)
        return open_link

    def two(self, server):
        server.call('register',project='demo',name='alpha',cwd=self.temp.name)
        server.call('register',project='demo',name='beta',cwd=self.temp.name,transport='claude-peer',session=self.sid)
        return [server.call('send',project='demo',sender='alpha',recipient='beta',body=f'Question {n}',key=f'k{n}')
                for n in (1,2)]

    def env(self):
        return mock.patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':self.temp.name,
                                           'CLAUDE_DESKTOP_SESSIONS_DIR':str(self.root/'desktop')})

    def first_in_thread(self, server, message, results):
        def run():
            try:
                results.append(bridge.push(server.path,message,wake_timeout=2))
            except bridge.PushFailed as error:
                results.append(error)
        thread = threading.Thread(target=run);thread.start()
        self.assertTrue(self.opening.wait(5))
        return thread

    def test_second_message_is_delivered_once_after_the_wake(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.slow()), \
                mock.patch.object(bridge,'TITLE_WAIT',0.0):
            first, second = self.two(server)
            results = []
            with mock.patch.object(bridge.broker,'request',wraps=bridge.broker.request) as calls:
                thread = self.first_in_thread(server,first,results)
                later = bridge.push(server.path,second,wake_timeout=2)
                thread.join(10)
            ops = [c.args[1]['op'] for c in calls.call_args_list]
            # Both claims, the waiting one included, use the caller-token claim (#118); no other flavour.
            self.assertEqual(ops.count('claim-guarded'),3)  # first, refused second, second after the wait
            self.assertNotIn('claim',ops)
            self.assertEqual(results[0]['state'],'submitted')
            self.assertEqual(results[0]['wake']['state'],'woken')
            self.assertEqual(later['state'],'submitted')
            self.assertEqual(later['behind'],{'id':first['id'],'state':'submitted'})
            self.assertNotIn('wake',later)  # It rode on the first wake; no second link.
            self.assertEqual(len(self.urls),1)
            frames = [user['message']['content'] for _,user in self.fakes[0].received]
            self.assertEqual(len(frames),2)
            self.assertEqual([sum(m['id'] in f for f in frames) for m in (first,second)],[1,1])

    def test_second_message_gets_a_behind_note_when_the_wake_fails(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.slow(socket=False)), \
                mock.patch.object(bridge,'push_frame') as frame:
            first, second = self.two(server)
            results = []
            thread = self.first_in_thread(server,first,results)
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path,second,wake_timeout=2)
            thread.join(10)
            self.assertIsInstance(results[0],bridge.PushFailed)
            # One wording with the broker's blocked-delivery note (#117), plus how the blocker ended.
            self.assertRegex(str(caught.exception),
                             rf"^pending behind {first['id']} since \S+ \(pending\); not sent")
            stored = server.call('show',id=second['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertIn(f"pending behind {first['id']}",stored['note'])
            self.assertEqual(server.call('show',id=first['id'])['state'],'pending')
            self.assertEqual(len(self.urls),1)
            frame.assert_not_called()

    def test_message_behind_an_uncertain_delivery_is_noted_without_waiting(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()), \
                mock.patch.object(bridge,'push_frame') as frame:
            first, second = self.two(server)
            server.call('claim',id=first['id'])
            server.call('finish',id=first['id'],state='uncertain',note='bytes may have reached the receiver')
            started = time.monotonic()
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path,second,wake_timeout=2)
            self.assertLess(time.monotonic()-started,1.5)
            since = server.call('show',id=first['id'])['dispatch_started_at']
            self.assertIn(f"pending behind {first['id']} since {since} (uncertain); not sent",str(caught.exception))
            shown = server.call('show',id=second['id'])
            self.assertEqual(shown['state'],'pending')
            self.assertEqual(shown['blocked_by']['id'],first['id'])
            self.assertEqual(self.urls,[])
            frame.assert_not_called()

    def race_note(self, legacy):
        """Blocker uncertain; while the note is in flight an operator settles it and another client claims."""
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()), \
                mock.patch.object(bridge,'push_frame') as frame:
            first, second = self.two(server)
            server.call('claim',id=first['id'])
            server.call('finish',id=first['id'],state='uncertain',note='bytes may have reached the receiver')

            def before_note():
                server.call('resolve',id=first['id'],state='done',note='operator checked the session')
                server.call('claim',id=second['id'])  # The concurrent deliverer wins the race.
            with NoteRace(self.temp.name,server,before_note,legacy) as proxy, \
                    self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(proxy.path,second,wake_timeout=2)
            stored = server.call('show',id=second['id'])
            self.assertEqual(stored['state'],'dispatching')  # Never turned back to pending.
            self.assertNotIn('resolve',proxy.operations)
            self.assertEqual(proxy.operations.count('note-pending'),1)
            result = caught.exception.message
            self.assertEqual(result['state'],'dispatching')
            self.assertFalse(result['noted'])
            self.assertTrue(result['note'].startswith(f"pending behind {first['id']} since "))
            self.assertIn('(uncertain); not sent',result['note'])
            self.assertNotIn('pending behind',json.dumps(stored['events']))  # Nothing durable.
            frame.assert_not_called()
            return result

    def test_older_broker_without_note_op_keeps_the_note_local(self):
        self.assertEqual(self.race_note(legacy=True)['note_scope'],'local')

    def test_note_skips_a_message_claimed_between_check_and_note(self):
        self.assertNotIn('note_scope',self.race_note(legacy=False))

    def test_note_is_durable_while_the_message_stays_pending(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(bridge,'push_frame') as frame:
            first, second = self.two(server)
            server.call('claim',id=first['id'])
            server.call('finish',id=first['id'],state='uncertain',note='bytes may have reached the receiver')
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path,second,wake_timeout=2)
            self.assertTrue(caught.exception.message['noted'])
            stored = server.call('show',id=second['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual(stored['events'][-1]['note'],str(caught.exception))
            frame.assert_not_called()


class NoteRace:
    """Relay broker calls; run `before` just ahead of note-pending, optionally as a broker without that op."""
    def __init__(self, root, server, before, legacy):
        self.path = str(Path(root)/'race.sock')
        self.server, self.before, self.legacy = server, before, legacy
        self.operations = []
        self.stop = threading.Event()

    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen()
        self.sock.settimeout(0.1)
        self.thread = threading.Thread(target=self.relay, daemon=True)
        self.thread.start()
        return self

    def relay(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn, conn.makefile('rb') as reader:
                args = json.loads(reader.readline())
                op = args.pop('op')
                self.operations.append(op)
                if op == 'note-pending':
                    self.before()
                if op == 'note-pending' and self.legacy:
                    response = {'error':'Unknown operation'}
                else:
                    try:
                        response = {'result':self.server.call(op, **args)}
                    except ValueError as error:
                        response = {'error':str(error)}
                conn.sendall(bridge.broker.wire(response))

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


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
