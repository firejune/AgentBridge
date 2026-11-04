import asyncio
import concurrent.futures
from contextlib import redirect_stdout
from io import StringIO
import json
import sqlite3
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
import broker
from support import RunningBroker
from test_codex_live import FakeDesktop


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state = broker.State()
        for name in ('alpha','beta'):
            self.call('register',project='demo',name=name,cwd='/tmp',transport='realtime')
            self.call('channel-bind',project='demo',name=name,owner=name,description=name)

    def call(self,op,**args):
        return self.state.action({'op':op,**args})

    def send(self,key='request',body='Question'):
        return self.call('send',project='demo',sender_channel='beta',channel='alpha',body=body,key=key)

    def test_idempotence_and_conflicting_content(self):
        first=self.send()
        self.assertEqual(self.send()['id'],first['id'])
        with self.assertRaisesRegex(ValueError,'different content'):
            self.send(body='Changed')

    def test_reply_freezes_both_addresses_and_conversation(self):
        first=self.send()
        for name in ('alpha','beta'):
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
            self.call('channel-bind',project='demo',name='alpha',owner='alpha',description='bad')
        with self.assertRaisesRegex(ValueError,'missing or closed'):
            self.state.route('another-project',channel='alpha')

    def test_closed_channel_refuses_new_requests_but_accepts_original_reply(self):
        first=self.send()
        self.call('channel-close',project='demo',name='beta',expect_generation=1)
        with self.assertRaises(ValueError):self.send('new')
        self.assertEqual(self.call('reply',id=first['id'],body='Done',key='reply')['channel'],'beta')

    def test_ack_and_progress_do_not_generate_answers(self):
        m=self.send()
        self.call('ack',id=m['id'])
        self.call('progress',id=m['id'],note='Reviewing')
        self.assertEqual(m['state'],'in_progress')
        self.assertFalse(self.state.answers[m['id']].is_set())
        self.assertEqual(len(self.state.messages),1)

    def test_slow_subscriber_gets_an_error_instead_of_silent_loss(self):
        sub={'route':broker.route_key(self.state.route('demo',channel='alpha')),
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
        # Native metadata is isolated too: protocol tests must never inspect the
        # real user's Codex configuration/store after a fake owner accepts.
        self.preview_path = patch.object(bridge.codex_preview, 'state_path',
                                        return_value=Path(self.server.temp.name)/'missing.sqlite')
        self.preview_path.start()
        self.addCleanup(self.preview_path.stop)

    def send(self,key='socket-request'):
        return self.server.call('send',project='demo',sender_channel='beta',channel='alpha',body='Question',key=key)

    def test_remap_cli_preserves_pins_and_existing_messages(self):
        previous, replacement = str(uuid.uuid4()), str(uuid.uuid4())
        route = self.server.call('register', project='demo', name='alpha', cwd=self.server.temp.name,
                                 transport='codex', session=previous)
        channel = self.server.call('channel-bind', project='demo', name='alpha', owner='alpha',
                                   description='pinned', expect_generation=1)
        send = dict(project='demo', sender='alpha', recipient='beta', body='Question')
        old_message = self.server.call('send', key='old', **send)
        command = ['--socket', self.server.path, 'remap', 'alpha', '--project', 'demo',
                   '--expect-session', previous, '--session', replacement]
        thread = {'id': replacement, 'cwd': route['cwd']}
        wrong_workspace = {'session': dict(thread, cwd=self.server.state_dir)}
        with patch.object(bridge.sessions, 'read_codex', return_value=wrong_workspace), \
                patch.object(bridge.codex_live, 'Client') as client, redirect_stdout(StringIO()):
            self.assertEqual(bridge.main(command), 1)
            client.assert_not_called()
        with FakeDesktop('no-owner') as desktop, patch.object(bridge.codex_live, 'endpoint', return_value=Path(desktop.path)), \
                patch.object(bridge.sessions, 'read_codex', return_value={'session': thread}), redirect_stdout(StringIO()):
            self.assertEqual(bridge.main(command), 1)
        with FakeDesktop() as desktop, patch.object(bridge.codex_live, 'endpoint', return_value=Path(desktop.path)), \
                patch.object(bridge.sessions, 'read_codex', return_value={'session': thread}), redirect_stdout(StringIO()) as stdout:
            self.assertEqual(bridge.main(command), 0)
            self.assertEqual(json.loads(stdout.getvalue()), dict(route, session=replacement))
            self.assertEqual([request['method'] for request in desktop.calls], ['initialize', 'thread-owner-discovery'])
            self.assertEqual(desktop.calls[-1]['params']['conversationId'], replacement)
        self.assertEqual(self.server.call('send', key='old', **send), old_message)
        self.assertEqual(self.server.call('send', key='new', **send)['return_route']['session'], replacement)
        self.assertEqual(self.send()['route'], channel['route'])
        reply = self.server.call('reply', id=old_message['id'], key='reply', body='Answer')
        self.assertEqual(reply['route']['session'], previous)
        snapshot = Path(self.server.state_dir, 'routes.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            self.server.call('remap', project='demo', name='alpha', session=str(uuid.uuid4()), expected_route=route)
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            expected = dict(route, session=replacement, executable='previous-executable')
            self.server.call('remap', project='demo', name='alpha', session=str(uuid.uuid4()), expected_route=expected)
        self.assertEqual(Path(self.server.state_dir, 'routes.json').read_bytes(), snapshot)

    def test_remap_cli_restores_registered_non_archived_codex_routes(self):
        sid, archived, missing, wrong = (str(uuid.uuid4()) for _ in range(4))
        registrations = [('demo', 'alpha', sid), ('other', 'mirror', sid),
                         ('other', 'archive', archived), ('other', 'missing', missing),
                         ('other', 'wrong', wrong)]
        for project, name, session in registrations:
            self.server.call('register', project=project, name=name, cwd=self.server.temp.name,
                             transport='codex', session=session)
        command = ['--socket', self.server.path, 'remap']
        snapshot = Path(self.server.state_dir, 'routes.json')
        before = snapshot.read_bytes()
        database = Path(self.server.temp.name, 'state_5.sqlite')
        connection = sqlite3.connect(database)
        with connection:
            connection.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER)')
            connection.executemany('INSERT INTO threads VALUES (?, ?, ?)', [
                (sid, self.server.temp.name, 0), (archived, self.server.temp.name, 1),
                (wrong, self.server.state_dir, 0)])
        connection.close()
        native_before = database.read_bytes()
        for behavior, status in [('active', 'already_live'), ('no-owner', 'reopened')]:
            with FakeDesktop(behavior) as desktop, \
                    patch.object(bridge.codex_live, 'endpoint', return_value=Path(desktop.path)), \
                    patch.object(bridge.codex_preview, 'state_path', return_value=database), \
                    patch.object(bridge.codex_new, 'open_link') as opener, redirect_stdout(StringIO()) as stdout:
                opener.side_effect = lambda *_a, **_k: setattr(desktop, 'behavior', 'active')
                self.assertEqual(bridge.main(command), 1)  # Missing and mismatched routes remain unresolved.
                rows = json.loads(stdout.getvalue())['routes']
                self.assertEqual({(row['project'], row['name']): row['status'] for row in rows},
                                 {('demo', 'alpha'): status, ('other', 'mirror'): status,
                                  ('other', 'archive'): 'archived', ('other', 'missing'): 'unresolved',
                                  ('other', 'wrong'): 'unresolved'})
                owner_requests = [r for r in desktop.calls if r['method'] != 'initialize']
                self.assertTrue(all(r['method'] == 'thread-owner-discovery' for r in owner_requests))
                if status == 'reopened':
                    self.assertEqual(opener.call_args.args, ('codex://threads/' + sid,))
                    self.assertEqual(opener.call_count, 1)
                    self.assertEqual([r['params']['conversationId'] for r in owner_requests], [sid, sid, sid])
                else:
                    opener.assert_not_called()
                    self.assertEqual([r['params']['conversationId'] for r in owner_requests], [sid, sid])
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertEqual(database.read_bytes(), native_before)

    def test_remap_refresh_does_not_reopen_again_when_owner_is_missing(self):
        with patch.object(bridge.codex_live, 'Client', side_effect=OSError), \
                patch.object(bridge.codex_new, 'open_link') as opener:
            with self.assertRaisesRegex(ValueError, 'Timed out waiting'):
                bridge.refresh_codex(str(uuid.uuid4()), timeout=0.01)
            self.assertEqual(opener.call_count, 1)

    def watch(self,name,**extra):
        stream=broker.stream(self.server.path,{'op':'watch','project':'demo','channel':name,**extra},timeout=3)
        self.addCleanup(stream.close)
        ready=next(stream)
        self.assertEqual(ready['type'],'subscribed')
        return stream,ready

    def test_two_clients_receive_request_and_reply_immediately(self):
        alpha,_=self.watch('alpha')
        beta,_=self.watch('beta')
        m=self.send()
        event=next(alpha)
        self.assertEqual(event['message']['id'],m['id'])
        answer=self.server.call('reply',id=m['id'],body='Answer',key='answer')
        self.assertEqual(next(beta)['message']['id'],answer['id'])
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
        watcher,ready=self.watch('alpha')
        watcher.close()
        m=self.send()
        replay,_=self.watch('alpha',after=ready['seq'],instance=ready['instance'])
        self.assertEqual(next(replay)['message']['id'],m['id'])
        failed=broker.stream(self.server.path,{'op':'watch','project':'demo','channel':'alpha','after':0,'instance':'old'},timeout=2)
        with self.assertRaisesRegex(ValueError,'instance changed'):next(failed)
        failed.close()

    def test_handoff_keeps_old_watch_bound_to_original_generation(self):
        old,_=self.watch('alpha')
        first=self.send()
        self.assertEqual(next(old)['message']['route']['generation'],1)
        self.server.call('register',project='demo',name='alpha',transport='realtime',session=str(uuid.uuid4()),cwd=self.server.temp.name)
        self.server.call('channel-bind',project='demo',name='alpha',owner='alpha',description='new',expect_generation=1)
        new,_=self.watch('alpha')
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
            '--from-channel','beta','--channel','alpha','--key','ask','--file','-','--timeout','3'],
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
        p=subprocess.run([sys.executable,bridge.__file__,'--socket',self.server.path,'serve','--state-dir',self.server.state_dir],capture_output=True,text=True,timeout=5)
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

    def fake(self,transport='claude',result='queued',exit_code=0):
        exe=Path(self.server.temp.name)/'fake-cli'
        exe.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
            f'Path({str(exe.with_suffix(".args"))!r}).write_text(json.dumps(sys.argv))\n'
            f'print({result!r})\nraise SystemExit({exit_code})\n')
        exe.chmod(0o700)
        self.server.call('register',project='demo',name='alpha',transport=transport,session=str(uuid.uuid4()),cwd=self.server.temp.name,executable=str(exe))
        self.server.call('channel-bind',project='demo',name='alpha',owner='alpha',description='cli',expect_generation=1)
        return exe

    def codex_route(self):
        self.server.call('register',project='demo',name='alpha',transport='codex',
            session=str(uuid.uuid4()),cwd=self.server.temp.name)
        self.server.call('channel-bind',project='demo',name='alpha',owner='alpha',
            description='desktop',expect_generation=1)

    def test_codex_live_delivery_is_exclusive_and_duplicate_safe(self):
        self.codex_route()
        m=self.send()
        with FakeDesktop() as desktop, patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC':desktop.path}):
            self.assertFalse(bridge.deliver(self.server.path,m['id'],dry_run=True)['sent'])
            self.assertEqual(bridge.deliver(self.server.path,m['id'])['state'],'submitted')
            with self.assertRaisesRegex(ValueError,'pending'):bridge.deliver(self.server.path,m['id'])
            self.assertEqual(bridge.push_if_peer(self.server.path,self.send())['state'],'submitted')
            self.assertEqual(len([c for c in desktop.calls if c.get('method')=='thread-follower-steer-turn']),1)

    def test_codex_preview_runs_only_after_acceptance_and_keeps_peer_payload(self):
        self.codex_route()
        m=self.send()
        def initialize(route, body):
            self.assertEqual(self.server.call('show',id=m['id'])['state'],'submitted')
            self.assertEqual(body,'Question')
            return {'status':'unavailable','warning':'native store locked'}
        with FakeDesktop() as desktop, patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC':desktop.path}), patch.object(bridge.codex_preview,'initialize',side_effect=initialize) as preview:
            result=bridge.deliver(self.server.path,m['id'])
            self.assertEqual(result['state'],'submitted')
            self.assertEqual(result['preview']['status'],'unavailable')
            preview.assert_called_once()
            mutation=next(c for c in desktop.calls if c.get('method')=='thread-follower-steer-turn')
            self.assertEqual(mutation['params']['input'],[])
            self.assertEqual(mutation['params']['toolOutput']['name'],'agent_bridge')
            self.assertIsNone(mutation['params']['toolOutput']['namespace'])
            self.assertIn('Question',mutation['params']['toolOutput']['output'])
        with patch.object(bridge.codex_preview,'initialize') as preview:
            dry=self.send('dry')
            with FakeDesktop() as desktop, patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC':desktop.path}):
                bridge.deliver(self.server.path,dry['id'],dry_run=True)
            preview.assert_not_called()

    def test_codex_unexpected_preview_error_preserves_accepted_result(self):
        self.codex_route()
        m=self.send()
        with FakeDesktop() as desktop, patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC':desktop.path}), patch.object(bridge.codex_preview,'initialize',side_effect=RuntimeError('SECRET_PREVIEW_ERROR')):
            result=bridge.deliver(self.server.path,m['id'])
            self.assertEqual(result['state'],'submitted')
            self.assertEqual(result['preview']['status'],'unavailable')
            self.assertNotIn('SECRET_PREVIEW_ERROR',str(result))
            self.assertEqual(self.server.call('show',id=m['id'])['state'],'submitted')
            self.assertEqual(len([c for c in desktop.calls if c.get('method')=='thread-follower-steer-turn']),1)

    def test_codex_uncertain_blocks_retry_and_other_messages_to_same_session(self):
        self.codex_route()
        m=self.send()
        with FakeDesktop('disconnect') as desktop, patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC':desktop.path}), patch.object(bridge.codex_preview,'initialize') as preview:
            with self.assertRaises(bridge.PushFailed) as raised:bridge.deliver(self.server.path,m['id'])
            self.assertEqual(raised.exception.message['state'],'uncertain')
            preview.assert_not_called()
            with self.assertRaises(ValueError):bridge.deliver(self.server.path,m['id'])
            second=self.send('second')
            with self.assertRaisesRegex(ValueError,'active or uncertain'):bridge.deliver(self.server.path,second['id'])

    def test_delivery_timeout_and_shell_literals(self):
        self.codex_route()
        body="' \" $(touch SHOULD_NOT_EXIST) `touch ALSO_NOT`\nsecond line </cross-session-message>"
        m=self.server.call('send',project='demo',sender_channel='beta',channel='alpha',body=body,key='literals')
        with FakeDesktop() as desktop, patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC':desktop.path}):
            self.assertEqual(bridge.deliver(self.server.path,m['id'])['state'],'submitted')
            mutation=next(c for c in desktop.calls if c.get('method')=='thread-follower-steer-turn')
            payload=mutation['params']['toolOutput']['output']
            self.assertIn(body,payload)
            self.assertIn(bridge.reply_command(m,self.server.path),payload)
        self.assertFalse((Path(self.server.temp.name)/'SHOULD_NOT_EXIST').exists())
        self.assertFalse((Path(self.server.temp.name)/'ALSO_NOT').exists())
        with FakeDesktop('timeout') as desktop, patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC':desktop.path}):
            late=self.send('timeout')
            with self.assertRaises(bridge.PushFailed) as raised:bridge.deliver(self.server.path,late['id'],timeout=0.05)
            self.assertEqual(raised.exception.message['state'],'uncertain')

    def test_codex_missing_owner_returns_pending_and_cli_nonzero(self):
        self.codex_route()
        with FakeDesktop('no-owner') as desktop:
            result=subprocess.run([sys.executable,bridge.__file__,'--socket',self.server.path,
                'send','--project','demo','--from-channel','beta','--channel','alpha',
                '--key','missing','--file','-'],input='SECRET_PEER_BODY',text=True,capture_output=True,
                env={**os.environ,'AGENT_BRIDGE_CODEX_IPC':desktop.path},timeout=3)
            self.assertEqual(result.returncode,1,result.stderr)
            failure=json.loads(result.stdout)
            self.assertEqual(failure['message']['state'],'pending')
            self.assertNotIn('SECRET_PEER_BODY',result.stdout)
            self.assertNotIn('SECRET_PEER_BODY',result.stderr)

    def test_codex_send_and_reply_automatically_push_and_preserve_reply_route(self):
        self.codex_route()
        with FakeDesktop() as desktop, patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC':desktop.path}):
            outgoing=self.server.call('send',project='demo',sender_channel='alpha',channel='beta',body='Question',key='outgoing')
            answer=self.server.call('reply',id=outgoing['id'],body='Answer',key='incoming')
            sent=bridge.push_if_peer(self.server.path,answer)
            self.assertEqual(sent['state'],'submitted')
            self.assertEqual(sent['route'],outgoing['return_route'])
            new=self.send('automatic')
            self.assertEqual(bridge.push_if_peer(self.server.path,new)['state'],'submitted')

    def test_codex_cancellation_records_uncertain(self):
        self.codex_route()
        m=self.send()
        with patch.object(bridge.codex_live,'deliver',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):bridge.deliver(self.server.path,m['id'])
        self.assertEqual(self.server.call('show',id=m['id'])['state'],'uncertain')

    def test_claude_response_notifies_waiter_without_auto_pingpong(self):
        self.fake('claude',json.dumps({'result':'Reviewed','is_error':False}))
        m=self.send()
        self.assertEqual(bridge.deliver(self.server.path,m['id'])['state'],'answered')
        result=self.server.call('wait',id=m['id'],timeout=0.1)
        self.assertEqual(result['replies'][0]['state'],'pending')
        self.assertEqual(result['replies'][0]['body'],'Reviewed')


if __name__=='__main__':
    unittest.main()
