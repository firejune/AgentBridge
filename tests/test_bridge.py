import asyncio
import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
import broker
from support import RunningBroker


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state = broker.State()
        for name in ('naru','roa'):
            self.call('register',project='demo',name=name,cwd='/tmp',transport='realtime')
            self.call('channel-bind',project='demo',name=name,owner=name,description=name)

    def call(self,op,**args):
        return self.state.action({'op':op,**args})

    def send(self,key='request',body='Question'):
        return self.call('send',project='demo',sender_channel='roa',channel='naru',body=body,key=key)

    def test_idempotence_and_conflicting_content(self):
        first=self.send()
        self.assertEqual(self.send()['id'],first['id'])
        with self.assertRaisesRegex(ValueError,'different content'):
            self.send(body='Changed')

    def test_reply_freezes_both_addresses_and_conversation(self):
        first=self.send()
        for name in ('naru','roa'):
            self.call('register',project='demo',name=name,transport='codex',session=str(uuid.uuid4()),cwd='/tmp')
            self.call('channel-bind',project='demo',name=name,owner=name,description='new',expect_generation=1)
        second=self.send('new')
        self.assertEqual(second['route']['generation'],2)
        answer=self.call('reply',id=first['id'],body='Answer',key='answer')
        self.assertEqual(answer['route'],first['return_route'])
        self.assertEqual(answer['return_route'],first['route'])
        self.assertEqual(answer['conversation'],first['conversation'])
        self.assertEqual(len(self.call('conversation',id=answer['id'])),2)
        self.assertEqual(self.send()['id'],first['id'])

    def test_generation_and_project_scope(self):
        with self.assertRaisesRegex(ValueError,'generation'):
            self.call('channel-bind',project='demo',name='naru',owner='naru',description='bad')
        with self.assertRaisesRegex(ValueError,'missing or closed'):
            self.state.route('another-project',channel='naru')

    def test_closed_channel_refuses_new_requests_but_accepts_original_reply(self):
        first=self.send()
        self.call('channel-close',project='demo',name='roa',expect_generation=1)
        with self.assertRaises(ValueError):self.send('new')
        self.assertEqual(self.call('reply',id=first['id'],body='Done',key='reply')['channel'],'roa')

    def test_ack_and_progress_do_not_generate_answers(self):
        m=self.send()
        self.call('ack',id=m['id'])
        self.call('progress',id=m['id'],note='Reviewing')
        self.assertEqual(m['state'],'in_progress')
        self.assertFalse(self.state.answers[m['id']].is_set())
        self.assertEqual(len(self.state.messages),1)

    def test_slow_subscriber_gets_an_error_instead_of_silent_loss(self):
        sub={'route':broker.route_key(self.state.route('demo',channel='naru')),
             'queue':asyncio.Queue(maxsize=1),'overflow':False}
        self.state.subscribers.append(sub)
        self.send('one');self.send('two');self.send('three')
        self.assertTrue(sub['overflow'])
        self.assertIn('fell behind',sub['queue'].get_nowait()['error'])
        self.assertTrue(sub['queue'].empty())

    def test_message_capacity_and_size_are_explicit(self):
        self.state.max_messages=1
        self.send()
        with self.assertRaisesRegex(ValueError,'limit reached'):self.send('second')
        with self.assertRaisesRegex(ValueError,'32768'):self.send('big','x'*32769)


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.server=RunningBroker().__enter__()
        self.addCleanup(self.server.__exit__,None,None,None)
        self.server.register_pair()

    def send(self,key='socket-request'):
        return self.server.call('send',project='demo',sender_channel='roa',channel='naru',body='Question',key=key)

    def watch(self,name,**extra):
        stream=broker.stream(self.server.path,{'op':'watch','project':'demo','channel':name,**extra},timeout=3)
        self.addCleanup(stream.close)
        ready=next(stream)
        self.assertEqual(ready['type'],'subscribed')
        return stream,ready

    def test_two_clients_receive_request_and_reply_immediately(self):
        naru,_=self.watch('naru')
        roa,_=self.watch('roa')
        m=self.send()
        event=next(naru)
        self.assertEqual(event['message']['id'],m['id'])
        answer=self.server.call('reply',id=m['id'],body='Answer',key='answer')
        self.assertEqual(next(roa)['message']['id'],answer['id'])
        result=self.server.call('wait',id=m['id'],timeout=0.1)
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['replies'][0]['id'],answer['id'])

    def test_wait_timeout_is_not_cancellation(self):
        m=self.send()
        result=self.server.call('wait',id=m['id'],timeout=0.02)
        self.assertTrue(result['timed_out'])
        self.assertEqual(self.server.call('show',id=m['id'])['state'],'pending')
        self.server.call('reply',id=m['id'],body='Late answer',key='late')
        self.assertFalse(self.server.call('wait',id=m['id'],timeout=0.02)['timed_out'])

    def test_reconnect_replays_events_and_rejects_wrong_instance(self):
        watcher,ready=self.watch('naru')
        watcher.close()
        m=self.send()
        replay,_=self.watch('naru',after=ready['seq'],instance=ready['instance'])
        self.assertEqual(next(replay)['message']['id'],m['id'])
        failed=broker.stream(self.server.path,{'op':'watch','project':'demo','channel':'naru','after':0,'instance':'old'},timeout=2)
        with self.assertRaisesRegex(ValueError,'instance changed'):next(failed)
        failed.close()

    def test_handoff_keeps_old_watch_bound_to_original_generation(self):
        old,_=self.watch('naru')
        first=self.send()
        self.assertEqual(next(old)['message']['route']['generation'],1)
        self.server.call('register',project='demo',name='naru',transport='realtime',session=str(uuid.uuid4()),cwd=self.server.temp.name)
        self.server.call('channel-bind',project='demo',name='naru',owner='naru',description='new',expect_generation=1)
        new,_=self.watch('naru')
        second=self.send('second')
        self.assertEqual(next(new)['message']['id'],second['id'])
        self.server.call('reply',id=first['id'],body='Old session answer',key='old-answer')
        event=next(old)
        self.assertEqual((event['type'],event['message']['route']['generation']),('status',1))

    def test_concurrent_duplicate_sends_emit_one_message(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids=list(pool.map(lambda _:self.send()['id'],range(8)))
        self.assertEqual(len(set(ids)),1)
        self.assertEqual(self.server.call('ping')['messages'],1)

    def test_cli_ask_and_reply_roundtrip(self):
        p=subprocess.Popen([sys.executable,bridge.__file__,'--socket',self.server.path,'ask','--project','demo',
            '--from-channel','roa','--channel','naru','--key','ask','--file','-','--timeout','3'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            p.stdin.write('Discuss API');p.stdin.close();p.stdin=None
            sent=json.loads(p.stdout.readline())
            self.assertEqual(sent['type'],'sent')
            self.server.call('reply',id=sent['message']['id'],body='Agreed',key='ask-response')
            out,err=p.communicate(timeout=5)
            self.assertEqual(p.returncode,0,err)
            self.assertEqual(json.loads(out)['replies'][0]['body'],'Agreed')
        finally:
            if p.poll() is None:p.kill();p.communicate()

    def test_single_broker_ownership_does_not_disconnect_existing_server(self):
        p=subprocess.run([sys.executable,bridge.__file__,'--socket',self.server.path,'serve'],capture_output=True,text=True,timeout=5)
        self.assertEqual(p.returncode,1)
        self.assertEqual(self.server.call('ping')['instance'],self.server.instance)

    def test_restart_has_no_persistent_messages(self):
        self.send()
        self.server.call('shutdown')
        self.server.process.communicate(timeout=5)
        with RunningBroker() as other:
            self.assertNotEqual(other.instance,self.server.instance)
            self.assertEqual(other.call('ping')['messages'],0)
        self.assertFalse(list(Path(self.server.temp.name).glob('*.db')))

    def fake(self,transport='codex',result='queued',exit_code=0):
        exe=Path(self.server.temp.name)/'fake-cli'
        exe.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
            f'Path({str(exe.with_suffix(".args"))!r}).write_text(json.dumps(sys.argv))\n'
            f'print({result!r})\nraise SystemExit({exit_code})\n')
        exe.chmod(0o700)
        self.server.call('register',project='demo',name='naru',transport=transport,session=str(uuid.uuid4()),cwd=self.server.temp.name,executable=str(exe))
        self.server.call('channel-bind',project='demo',name='naru',owner='naru',description='cli',expect_generation=1)
        return exe

    def test_external_cli_delivery_remains_explicit_and_exclusive(self):
        exe=self.fake()
        m=self.send()
        self.assertFalse(bridge.deliver(self.server.path,m['id'],dry_run=True)['sent'])
        self.assertFalse(exe.with_suffix('.args').exists())
        self.assertEqual(bridge.deliver(self.server.path,m['id'])['state'],'submitted')
        with self.assertRaisesRegex(ValueError,'pending'):bridge.deliver(self.server.path,m['id'])

    def test_cli_failure_blocks_retry_and_other_messages_to_same_session(self):
        self.fake(exit_code=2)
        m=self.send()
        self.assertEqual(bridge.deliver(self.server.path,m['id'])['state'],'uncertain')
        with self.assertRaises(ValueError):bridge.deliver(self.server.path,m['id'])
        second=self.send('second')
        with self.assertRaisesRegex(ValueError,'active or uncertain'):bridge.deliver(self.server.path,second['id'])

    def test_delivery_timeout_and_shell_literals(self):
        exe=self.fake()
        body="' \" $(touch SHOULD_NOT_EXIST) `touch ALSO_NOT`\nsecond line"
        m=self.server.call('send',project='demo',sender_channel='roa',channel='naru',body=body,key='literals')
        self.assertEqual(bridge.deliver(self.server.path,m['id'])['state'],'submitted')
        argv=json.loads(exe.with_suffix('.args').read_text())
        self.assertTrue(argv[-1].endswith(body))
        self.assertFalse((exe.parent/'SHOULD_NOT_EXIST').exists())
        self.assertFalse((exe.parent/'ALSO_NOT').exists())
        exe.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(10)\n')
        late=self.send('timeout')
        self.assertEqual(bridge.deliver(self.server.path,late['id'],timeout=0.05)['state'],'uncertain')

    def test_claude_response_notifies_waiter_without_auto_pingpong(self):
        self.fake('claude',json.dumps({'result':'Reviewed','is_error':False}))
        m=self.send()
        self.assertEqual(bridge.deliver(self.server.path,m['id'])['state'],'answered')
        result=self.server.call('wait',id=m['id'],timeout=0.1)
        self.assertEqual(result['replies'][0]['state'],'pending')
        self.assertEqual(result['replies'][0]['body'],'Reviewed')


if __name__=='__main__':
    unittest.main()
