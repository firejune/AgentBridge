"""No real session discovery, IDE opening, or peer delivery in these tests."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'skill/agent-bridge/scripts'))
import mcp_phase1 as phase
import mcp_server
import broker
from support import RunningBroker
try:
    import jsonschema
except ImportError:
    jsonschema = None


class FakeBackend:
    def __init__(self):
        self.current = 'instance1'; self.creates=0; self.posts=0; self.pushes=0
        self.messages={}; self.replies={}; self.register_error=False; self.crash=None; self.gate=None
    def instance(self): return self.current
    def create(self,workspace):
        self.creates+=1
        if self.gate: self.gate.wait(3)
        if self.crash=='create': raise RuntimeError('sensitive path /SECRET')
        return str(uuid.uuid4())
    def register(self,s):
        if self.register_error: raise ValueError('private token')
    def availability(self,s): return 'ready'
    def post(self,op,s):
        self.posts+=1
        mid=str(uuid.uuid4()); m={'id':mid,'state':'pending'}; self.messages[mid]=m
        if self.crash=='post': raise RuntimeError('response lost')
        return m.copy()
    def push(self,m,s):
        self.pushes+=1; self.messages[m['id']]['state']='submitted'
        if self.crash=='push': raise RuntimeError('response lost')
        return self.messages[m['id']].copy()
    def retire_reply(self,mid,parent): pass
    def observe(self,mid):
        return self.messages[mid].copy(),self.replies.get(mid,[])


class PhaseTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mcp-phase1-',dir='/tmp')
        self.root=Path(self.temp.name).resolve()
        self.config={'principal':'owner-a','tunnel_id':'approved-test-tunnel','writes_enabled':True,
                     'scopes':list(set(phase.SCOPES.values())), 'state_dir':str(self.root/'state'),
                     'workspaces':{'test':{'cwd':str(self.root),'root':str(self.root),'project':'mcp-test','ide':sessions_ide()}}}
        self.backend=FakeBackend(); self.engine=phase.Engine(self.config,self.backend,start=False)
    def tearDown(self):
        self.engine.close(); self.temp.cleanup()
    def call(self,name,**args):
        out=self.engine.call(name,args)
        if jsonschema:
            jsonschema.validate(out,next(t['outputSchema'] for t in phase.TOOLS if t['name']==name))
        return out
    def create(self,key='create_key_0000001'):
        result=self.call('bridge_session_create',workspace_alias='test',idempotency_key=key)['data']
        self.engine.tick()
        return self.call('bridge_operation_get',operation_id=result['operation_id'])['data']
    def send(self,sid,key='message_key_000001',text='hello'):
        return self.call('bridge_message_send',agent_session_id=sid,message=text,idempotency_key=key)
    def restart(self):
        self.engine.close(); self.engine=phase.Engine(self.config,self.backend,start=False)


    def test_ledger_namespace_restart_and_legacy_key_compatibility(self):
        ns=self.engine.ledger_namespace
        sid=self.create()['agent_session_id']; a=self.send(sid)['data']
        op=self.engine.operation(a['operation_id'],'owner-a')
        downstream=op['downstream_key']
        self.assertNotEqual(downstream,op['key_hash'])
        self.restart()
        self.assertEqual(self.engine.ledger_namespace,ns)
        self.assertEqual(self.engine.operation(op['operation_id'],'owner-a')['downstream_key'],downstream)
        legacy=dict(op);legacy.pop('downstream_key')
        backend=phase.Backend('/unused');session=self.engine.session(sid,'owner-a')
        backend.verify_routes=lambda s:None
        keys=[]
        def rpc(name,**kw):
            keys.append(kw['key'])
            return {'route':{'session':session['uuid'],'transport':'claude-peer'}}
        backend.rpc=rpc;backend.post(legacy,session)
        self.assertEqual(keys,[op['key_hash']])

    def test_reply_cleanup_failure_keeps_success_and_resumes_without_send(self):
        sid=self.create()['agent_session_id'];a=self.send(sid)['data'];self.engine.tick()
        op=self.engine.operation(a['operation_id'],'owner-a')
        self.backend.replies[op['backend_id']]=[{'id':'r1','reply_to':op['backend_id'],'created':1,'body':'durable'}]
        def fail(mid,parent):raise OSError('temporary outage')
        self.backend.retire_reply=fail;self.engine.tick()
        saved=self.engine.operation(op['operation_id'],'owner-a')
        self.assertEqual(saved['status'],'succeeded')
        self.assertEqual(saved['pending_reply_cleanup'],['r1'])
        self.restart();calls=[]
        self.backend.retire_reply=lambda mid,parent:calls.append((mid,parent))
        self.engine.tick()
        self.assertEqual(calls,[('r1',op['backend_id'])])
        self.assertEqual(self.backend.pushes,1)
        self.assertEqual(self.engine.operation(op['operation_id'],'owner-a')['pending_reply_cleanup'],[])

    def grant(self):
        self.config['pinned_sessions']={'hq':{'uuid':str(uuid.uuid4()),'cwd':str(self.root),
            'project':'hq-project','route':'hq-route','authorized_at':phase.stamp()}}
        return next(iter(phase.pinned_sessions(self.config)))

    def test_pinned_busy_send_and_replay_without_create(self):
        sid=self.grant()
        self.backend.availability=lambda s:'busy'
        accepted=self.send(sid)['data']
        self.engine.tick()
        self.assertEqual((self.backend.creates,self.backend.posts,self.backend.pushes),(0,1,1))
        self.assertTrue(self.send(sid)['data']['replayed'])
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM owned_sessions').fetchone()[0],0)
        self.assertEqual(self.engine.call('bridge_message_send',{'agent_session_id':sid,
            'message':'x','idempotency_key':'different-owner-key'},principal='other')['error']['code'],'NOT_AUTHORIZED')
        op=self.engine.operation(accepted['operation_id'],'owner-a')
        self.backend.replies[op['backend_id']]=[{'id':'reply','reply_to':op['backend_id'],'created':1,'body':'response'}]
        self.engine.tick()
        self.assertEqual(self.engine.get({'operation_id':op['operation_id']},'owner-a')['result']['text'],'response')

    def test_pinned_revocation_and_retarget_cannot_dispatch(self):
        sid=self.grant(); self.send(sid)
        self.config['pinned_sessions']['hq']['uuid']=str(uuid.uuid4())
        self.assertEqual(self.send(sid)['error']['code'],'NOT_FOUND')
        self.engine.tick()
        self.assertEqual(self.backend.posts,0)
        self.config.pop('pinned_sessions')
        self.assertEqual(self.send(sid)['error']['code'],'NOT_FOUND')

    def test_pinned_list_not_creation_alias_and_restart_stable(self):
        sid=self.grant(); self.restart()
        rows=self.call('bridge_sessions_list')['data']['sessions']
        self.assertEqual(rows[0]['agent_session_id'],sid)
        self.assertEqual(self.call('bridge_session_create',workspace_alias='hq',
            idempotency_key='no-create-hq-0001')['error']['code'],'WORKSPACE_NOT_ALLOWED')

    def test_pinned_backend_requires_exact_existing_route(self):
        sid=self.grant(); session=phase.pinned_sessions(self.config)[sid]
        backend=phase.Backend('/unused')
        target={'name':session['route'],'transport':'claude-peer','session':session['uuid'],'cwd':str(self.root)}
        calls=[]
        def rpc(op,**kw):
            calls.append((op,kw))
            if op=='routes': return [target]
        backend.rpc=rpc
        with patch.object(phase.sessions,'resolve_claude_peer',return_value={'id':session['uuid'],'cwd':str(self.root)}):
            backend.check(session)
            target['session']=str(uuid.uuid4())
            with self.assertRaises(ValueError):backend.register(session)
        self.assertFalse(any(op=='register' for op,args in calls))

    def test_pinned_config_rejects_alias_collision_and_invalid_cwd(self):
        self.grant()
        self.config['pinned_sessions']['test']=self.config['pinned_sessions'].pop('hq')
        with self.assertRaises(ValueError):phase.pinned_sessions(self.config)
    def test_durable_concurrent_replay_and_conflict(self):
        args={'workspace_alias':'test','idempotency_key':'same_create_000001'}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.engine.call('bridge_session_create',args),range(20)))
        self.assertEqual(len({r['data']['operation_id'] for r in results}),1)
        self.assertEqual(sum(not r['data']['replayed'] for r in results),1)
        self.restart(); self.engine.tick(); self.assertEqual(self.backend.creates,1)
        replay=self.engine.call('bridge_session_create',args)['data']
        self.assertTrue(replay['replayed']); self.assertEqual(replay['accepted_at'],results[0]['data']['accepted_at'])
        sid=self.engine.get({'operation_id':replay['operation_id']},'owner-a')['agent_session_id']
        sent=self.send(sid); again=self.send(sid)
        self.assertTrue(again['data']['replayed']); self.assertEqual(sent['data']['operation_id'],again['data']['operation_id'])
        self.assertEqual(self.send(sid,text='different')['error']['code'],'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.send(sid,key='other_message_0001')['error']['code'],'SESSION_BUSY')
    def test_post_push_reply_and_immutable_pagination(self):
        sid=self.create()['agent_session_id']; accepted=self.send(sid,text='  exact 한글  ')['data']
        self.engine.tick(); self.assertEqual((self.backend.posts,self.backend.pushes),(1,1))
        oid=accepted['operation_id']; op=self.engine.operation(oid,'owner-a')
        self.assertEqual(op['message'],'  exact 한글  ')
        self.assertEqual(self.call('bridge_operation_get',operation_id=oid)['data']['status'],'running')
        self.backend.messages[op['backend_id']]['state']='answered'; self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'running')
        self.backend.replies[op['backend_id']]=[{'id':'b','created':'2','reply_to':op['backend_id'],'body':'🌈끝'},{'id':'a','created':'1','reply_to':op['backend_id'],'body':'한글'}]
        self.engine.tick(); first=self.call('bridge_operation_get',operation_id=oid,max_chars=3)['data']['result']
        self.assertEqual(first['text'],'한글\n');self.assertTrue(first['has_more'])
        self.restart()
        second=self.call('bridge_operation_get',operation_id=oid,cursor=first['next_cursor'])['data']['result']
        self.assertEqual(second['text'],'\n🌈끝');self.assertFalse(second['has_more'])
        self.backend.replies[op['backend_id']].append({'id':'c','created':'3','body':'late'})
        self.engine.tick(); self.assertNotIn('late',self.engine.get({'operation_id':oid},'owner-a')['result']['text'])
    def test_input_ownership_and_cursor(self):
        sid=self.create()['agent_session_id']
        self.assertEqual(self.send(sid,text='한'*12000)['error']['code'],'INVALID_ARGUMENT')
        self.assertEqual(self.send(sid,text='  ')['error']['code'],'INVALID_ARGUMENT')
        self.assertEqual(self.call('bridge_session_create',workspace_alias='../hq',idempotency_key='valid_key_0000001')['error']['code'],'INVALID_ARGUMENT')
        self.assertEqual(self.call('bridge_session_create',workspace_alias='hq',idempotency_key='valid_key_0000001')['error']['code'],'WORKSPACE_NOT_ALLOWED')
        self.assertEqual(self.engine.call('bridge_sessions_list',{},'other')['error']['code'],'NOT_AUTHORIZED')
        with self.assertRaises(phase.Fault) as e: self.engine.session(sid,'other')
        self.assertEqual(e.exception.payload['code'],'NOT_FOUND')
        oid=self.send(sid)['data']['operation_id']
        with self.assertRaises(phase.Fault): self.engine.operation(oid,'other')
        for args in ({'operation_id':oid,'wait_ms':True},{'operation_id':oid,'wait_ms':5001},{'operation_id':oid,'cursor':'forged'}):
            self.assertEqual(self.engine.call('bridge_operation_get',args)['error']['code'],'INVALID_ARGUMENT')
        self.assertEqual(self.call('bridge_session_create',workspace_alias='test',idempotency_key='valid_key_0000001',model='x')['error']['code'],'INVALID_ARGUMENT')
        token=self.engine.cursor('owner-a','result',oid,1)
        with self.assertRaises(phase.Fault): self.engine.offset(token,'owner-b','result',oid)
        with self.assertRaises(phase.Fault): self.engine.offset(token,'owner-a','result','another-op')
    def test_crashes_never_repeat_effects(self):
        for point in ('create','post','push'):
            with self.subTest(point=point):
                if point=='create':
                    self.backend.crash=point; accepted=self.call('bridge_session_create',workspace_alias='test',idempotency_key='crash_create_0001')['data']
                else:
                    self.backend.crash=None; sid=self.create('create_for_'+point+'_001')['agent_session_id']
                    self.backend.crash=point; accepted=self.send(sid,key='crash_message_'+point+'_001')['data']
                self.engine.tick(); oid=accepted['operation_id']
                self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'unknown')
                counts=(self.backend.creates,self.backend.posts,self.backend.pushes)
                self.restart(); self.engine.tick()
                self.assertEqual((self.backend.creates,self.backend.posts,self.backend.pushes),counts)
                self.assertNotIn('SECRET',json.dumps(self.call('bridge_operation_get',operation_id=oid)))
    def test_known_creation_registration_recovery(self):
        self.backend.register_error=True; op=self.create()
        self.assertEqual(op['status'],'unknown'); self.assertIsNotNone(op['agent_session_id'])
        self.assertEqual(self.send(op['agent_session_id'])['error']['code'],'BACKEND_UNAVAILABLE')
        self.restart(); self.backend.register_error=False
        result=self.engine.recover_registration(op['operation_id'])
        self.assertEqual(result['status'],'succeeded'); self.assertEqual(self.backend.creates,1)
    def test_recovery_observes_only_and_broker_loss_blocks(self):
        sid=self.create()['agent_session_id']; oid=self.send(sid)['data']['operation_id']; self.engine.tick()
        self.restart(); self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'running')
        self.assertEqual(self.backend.pushes,1)
        self.backend.current='restarted'; self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'unknown')
        self.assertEqual(self.send(sid,key='new_message_00001')['error']['code'],'SESSION_BUSY')
    def test_posted_restart_is_unknown_not_pushed(self):
        sid=self.create()['agent_session_id']; oid=self.send(sid)['data']['operation_id']
        op=self.engine.operation(oid,'owner-a'); m=self.backend.post(op,{})
        self.engine.update(op,status='running',stage='posted',backend_id=m['id'],instance=self.backend.current)
        self.restart(); self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'unknown')
        self.assertEqual(self.backend.pushes,0)
    def test_evicted_message_unknown(self):
        sid=self.create()['agent_session_id']; oid=self.send(sid)['data']['operation_id']; self.engine.tick()
        self.backend.messages.clear(); self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'unknown')
    def test_tombstone_and_private_permissions(self):
        op=self.create(); stored=self.engine.operation(op['operation_id'],'owner-a')
        self.engine.update(stored,finished_epoch=time.time()-phase.RETENTION-1); self.engine.tick()
        self.assertEqual(self.call('bridge_operation_get',operation_id=op['operation_id'])['error']['code'],'OPERATION_EXPIRED')
        self.assertEqual(self.call('bridge_session_create',workspace_alias='test',idempotency_key='create_key_0000001')['error']['code'],'IDEMPOTENCY_EXPIRED')
        self.assertEqual(self.backend.creates,1)
        self.assertEqual((self.root/'state/ledger.sqlite3').stat().st_mode&0o777,0o600)
        with self.assertRaises(ValueError): phase.Engine(self.config,self.backend,start=False)
    def test_async_accept_and_wait_bound(self):
        self.backend.gate=threading.Event()
        self.engine.thread=threading.Thread(target=self.engine.run,daemon=True); self.engine.thread.start()
        start=time.monotonic()
        op=self.call('bridge_session_create',workspace_alias='test',idempotency_key='async_create_0001')['data']
        self.assertLess(time.monotonic()-start,2)
        start=time.monotonic(); self.engine.get({'operation_id':op['operation_id'],'wait_ms':100},'owner-a')
        self.assertLess(time.monotonic()-start,.3)
        self.backend.gate.set()
    def test_mcp_opt_in_and_stdio(self):
        req={'jsonrpc':'2.0','id':1,'method':'tools/list'}
        self.assertEqual(len(mcp_server.handle(req,'/missing')['result']['tools']),1)
        self.assertEqual(len(mcp_server.handle(req,'/missing',self.engine)['result']['tools']),5)
        call={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'bridge_session_create','arguments':{'workspace_alias':'test','idempotency_key':'stdio_create_001'}}}
        output=io.StringIO();mcp_server.serve_stdio('/missing',io.StringIO(json.dumps(call)+'\n'),output,self.engine)
        result=json.loads(output.getvalue())['result']
        self.assertEqual(json.loads(result['content'][0]['text']),result['structuredContent'])
        self.assertTrue(result['structuredContent']['ok'])
    def test_wait_max_is_not_a_backend_wait(self):
        accepted=self.call('bridge_session_create',workspace_alias='test',idempotency_key='wait_max_00000001')['data']
        start=time.monotonic()
        result=self.engine.get({'operation_id':accepted['operation_id'],'wait_ms':5000},'owner-a')
        self.assertLess(time.monotonic()-start,5.2)
        self.assertEqual(result['status'],'accepted');self.assertEqual(self.backend.creates,0)

    def test_alias_snapshot_and_metadata_only_list(self):
        op=self.create(); listing=self.call('bridge_sessions_list')['data']
        self.assertEqual(len(listing['sessions']),1)
        self.assertNotIn(str(self.root),json.dumps(listing))
        self.assertNotIn('uuid',json.dumps(listing))
        self.config['workspaces']['test']['project']='changed'
        self.assertEqual(self.send(op['agent_session_id'])['error']['code'],'WORKSPACE_NOT_ALLOWED')
        self.assertEqual(self.call('bridge_sessions_list')['data']['sessions'][0]['state'],'unavailable')

    def test_pending_intent_crash_and_owner_binding(self):
        op=self.call('bridge_session_create',workspace_alias='test',idempotency_key='intent_create_001')['data']
        row=self.engine.operation(op['operation_id'],'owner-a')
        self.engine.update(row,status='running',stage='create_intent')
        self.restart()
        self.assertEqual(self.engine.operation(op['operation_id'],'owner-a')['status'],'unknown')
        self.assertEqual(self.backend.creates,0)
        self.engine.close()
        altered=dict(self.config,principal='other')
        with self.assertRaises(ValueError): phase.Engine(altered,self.backend,start=False)
        self.engine=phase.Engine(self.config,self.backend,start=False)

    def test_persistence_failure_prevents_dispatch(self):
        accepted=self.call('bridge_session_create',workspace_alias='test',idempotency_key='disk_create_00001')['data']
        with patch.object(self.engine,'save',side_effect=OSError('disk full')):
            self.engine.tick()
        self.assertEqual(self.backend.creates,0)
        self.assertTrue(self.engine.stop.is_set())
        self.assertEqual(self.engine.operation(accepted['operation_id'],'owner-a')['status'],'accepted')

    def test_config_permissions_and_http_write_refusal(self):
        p=self.root/'config.json';p.write_text(json.dumps(self.config));p.chmod(0o644)
        with self.assertRaises(ValueError):phase.load_config(p)
        p.chmod(0o600);self.assertEqual(phase.load_config(p)['principal'],'owner-a')
        with self.assertRaises(SystemExit),patch('sys.stderr',io.StringIO()):
            mcp_server.main(['--http','0','--phase1-config',str(p)])

    def test_schema_contracts(self):
        if jsonschema:
            for tool in phase.TOOLS:
                jsonschema.Draft202012Validator.check_schema(tool['inputSchema'])
                jsonschema.Draft202012Validator.check_schema(tool['outputSchema'])

    def test_create_agy_and_codex_sessions_and_list_agent_types(self):
        self.assertEqual(self.call('bridge_session_create', workspace_alias='test',
            agent_type='unknown', idempotency_key='bad_agent_type_01')['error']['code'], 'INVALID_ARGUMENT')
        op_agy = self.call('bridge_session_create', workspace_alias='test',
            agent_type='agy', idempotency_key='create_agy_000001')['data']
        self.assertEqual(self.call('bridge_session_create', workspace_alias='test',
            agent_type='codex', idempotency_key='create_agy_000001')['error']['code'], 'IDEMPOTENCY_CONFLICT')
        op_codex = self.call('bridge_session_create', workspace_alias='test',
            agent_type='codex', idempotency_key='create_codex_0001')['data']
        self.engine.tick()
        sid_agy = self.call('bridge_operation_get', operation_id=op_agy['operation_id'])['data']['agent_session_id']
        sid_codex = self.call('bridge_operation_get', operation_id=op_codex['operation_id'])['data']['agent_session_id']
        rows = {r['agent_session_id']: r for r in self.call('bridge_sessions_list')['data']['sessions']}
        self.assertEqual(rows[sid_agy]['agent_type'], 'agy')
        self.assertEqual(rows[sid_codex]['agent_type'], 'codex')


def sessions_ide():
    return next(iter(phase.sessions.CLAUDE_IDES))


class BrokerIntegrationTest(unittest.TestCase):
    def test_real_broker_post_push_and_correlated_reply(self):
        with RunningBroker() as running, tempfile.TemporaryDirectory(prefix='mcp-',dir='/tmp') as temp:
            root=str(Path(temp).resolve())
            config={'principal':'test-owner','tunnel_id':'isolated-test','writes_enabled':True,
                    'scopes':list(set(phase.SCOPES.values())),'state_dir':root+'/state',
                    'workspaces':{'test':{'cwd':root,'root':root,'project':'mcp-test','ide':sessions_ide()}}}
            backend=phase.Backend(running.path); engine=phase.Engine(config,backend,start=False)
            sid=str(uuid.uuid4())
            row={'id':sid,'cwd':root,'status':'idle'}
            try:
                with patch.object(phase.sessions,'open_session',return_value={'session':row}) as opening, patch.object(phase.sessions,'resolve_claude_peer',return_value=row), patch.object(phase.bridge,'new_session_policy'):
                    accepted=engine.call('bridge_session_create',{'workspace_alias':'test','idempotency_key':'integrate_create01'})['data'];engine.tick()
                    op=engine.operation(accepted['operation_id'],'test-owner');self.assertEqual(op['status'],'succeeded')
                    opening.assert_called_once()
                    sent=engine.call('bridge_message_send',{'agent_session_id':op['agent_session_id'],'message':'hello','idempotency_key':'integrate_message1'})['data']
                    def mock_peer(path,m):
                        self.assertEqual(m['route']['session'],sid)
                        running.call('claim',id=m['id'])
                        return running.call('finish',id=m['id'],state='submitted',event='pushed')
                    with patch.object(phase.bridge,'push_if_peer',side_effect=mock_peer) as pushing:
                        engine.tick();pushing.assert_called_once()
                    op=engine.operation(sent['operation_id'],'test-owner');self.assertEqual(op['status'],'running')
                    running.call('reply',id=op['backend_id'],body='actual correlated reply',key='reply-key')
                    engine.tick();result=engine.get({'operation_id':op['operation_id']},'test-owner')
                    self.assertEqual(result['result']['text'],'actual correlated reply')
                    self.assertEqual(result['status'],'succeeded')
            finally:
                engine.close()

    def test_multi_provider_discovery_pinned_and_send(self):
        with RunningBroker() as running, tempfile.TemporaryDirectory(prefix='mcp-multi-', dir='/tmp') as temp:
            root = str(Path(temp).resolve())
            wt = Path(root) / 'wt-jinni'
            wt.mkdir()
            wt_str = str(wt.resolve())
            agy_sid = str(uuid.uuid4())
            codex_sid = str(uuid.uuid4())
            claude_sid = str(uuid.uuid4())
            pinned_agy_sid = str(uuid.uuid4())
            outside_sid = str(uuid.uuid4())

            running.call('register', project='mcp-test', name='jinni-hq',
                         cwd=root, transport='agy', session=pinned_agy_sid)

            config = {
                'principal': 'test-owner',
                'tunnel_id': 'isolated-multi',
                'writes_enabled': True,
                'scopes': list(set(phase.SCOPES.values())),
                'state_dir': root + '/state',
                'workspaces': {
                    'test': {'cwd': root, 'root': root, 'project': 'mcp-test', 'ide': sessions_ide()},
                },
                'pinned_sessions': {
                    'jinni-pin': {
                        'uuid': pinned_agy_sid,
                        'cwd': root,
                        'project': 'mcp-test',
                        'route': 'jinni-hq',
                        'authorized_at': phase.stamp(),
                    },
                },
            }
            backend = phase.Backend(running.path)
            engine = phase.Engine(config, backend, start=False)

            agy_rows = [
                {'provider': 'agy', 'id': agy_sid, 'cwd': wt_str, 'status': 'running', 'updated_at': '2026-11-01T00:00:00Z'},
                {'provider': 'agy', 'id': pinned_agy_sid, 'cwd': root, 'status': 'idle', 'updated_at': '2026-11-01T00:00:00Z'},
                {'provider': 'agy', 'id': outside_sid, 'cwd': '/tmp', 'status': 'idle', 'updated_at': '2026-11-01T00:00:00Z'},
            ]
            codex_rows = [
                {'provider': 'codex', 'id': codex_sid, 'cwd': root, 'status': 'idle', 'updated_at': 1730419200},
            ]
            claude_rows = [
                {'provider': 'claude', 'id': claude_sid, 'cwd': root, 'status': 'idle'},
            ]
            by_agy = {r['id']: r for r in agy_rows}

            try:
                with patch.object(phase.sessions, 'list_agy_peers', return_value=agy_rows), \
                        patch.object(phase.sessions, 'list_codex_peers', return_value=codex_rows), \
                        patch.object(phase.sessions, 'list_claude_peers', return_value=claude_rows), \
                        patch.object(phase.sessions, 'resolve_agy_peer', side_effect=lambda sid, **kw: by_agy[sid]), \
                        patch.object(phase.sessions, 'resolve_codex_peer', return_value=codex_rows[0]), \
                        patch.object(phase.sessions, 'resolve_claude_peer', return_value=claude_rows[0]):
                    listed = engine.call('bridge_sessions_list', {})['data']['sessions']
                    self.assertEqual(len(listed), 4)
                    by_type = {}
                    for item in listed:
                        by_type.setdefault(item['agent_type'], []).append(item)
                    self.assertEqual(len(by_type['agy']), 2)
                    self.assertEqual(len(by_type['codex']), 1)
                    self.assertEqual(len(by_type['claude']), 1)

                    busy_agy = next(item for item in by_type['agy'] if item['state'] == 'busy')
                    sent = engine.call('bridge_message_send', {
                        'agent_session_id': busy_agy['agent_session_id'],
                        'message': 'ping jinni worker',
                        'idempotency_key': 'multi_send_agy_001',
                    })['data']

                    def mock_peer(path, m):
                        self.assertEqual(m['route']['session'], agy_sid)
                        self.assertEqual(m['route']['transport'], 'agy')
                        running.call('claim', id=m['id'])
                        return running.call('finish', id=m['id'], state='submitted', event='pushed')

                    with patch.object(phase.bridge, 'push_if_peer', side_effect=mock_peer) as pushing:
                        engine.tick()
                        pushing.assert_called_once()

                    op = engine.operation(sent['operation_id'], 'test-owner')
                    self.assertEqual(op['status'], 'running')
                    running.call('reply', id=op['backend_id'], body='jinni reply ok', key='jinni-reply-1')
                    engine.tick()
                    res = engine.get({'operation_id': op['operation_id']}, 'test-owner')
                    self.assertEqual(res['status'], 'succeeded')
                    self.assertEqual(res['result']['text'], 'jinni reply ok')
            finally:
                engine.close()


class ReviewRegressionTest(unittest.TestCase):
    def exercise(self, count, ledgers, capacity):
        with RunningBroker(max_messages=capacity) as running, tempfile.TemporaryDirectory(dir='/tmp') as temp:
            root=str(Path(temp).resolve()); engines=[]
            config={'principal':'same-owner','tunnel_id':'same-tunnel','writes_enabled':True,
                    'scopes':list(set(phase.SCOPES.values())),
                    'workspaces':{'test':{'cwd':root,'root':root,'project':'test-project','ide':sessions_ide()}}}
            row={'id':str(uuid.uuid4()),'cwd':root,'status':'idle'}
            try:
                with patch.object(phase.sessions,'open_session',return_value={'session':row}), patch.object(phase.sessions,'resolve_claude_peer',return_value=row), patch.object(phase.bridge,'new_session_policy'):
                    for n in range(ledgers):
                        c=dict(config,state_dir=root+'/ledger'+str(n))
                        e=phase.Engine(c,phase.Backend(running.path),start=False); engines.append(e)
                        a=e.call('bridge_session_create',{'workspace_alias':'test','idempotency_key':'same-create-key-0001'})['data'];e.tick()
                        e.sid=e.operation(a['operation_id'],'same-owner')['agent_session_id']
                    for n in range(count):
                        e=engines[n % ledgers]
                        key='same-message-key-0001' if ledgers>1 else 'message-key-00000'+str(n)
                        a=e.call('bridge_message_send',{'agent_session_id':e.sid,'message':'hello','idempotency_key':key})['data']
                        def push(path,m):
                            running.call('claim',id=m['id'])
                            return running.call('finish',id=m['id'],state='submitted')
                        with patch.object(phase.bridge,'push_if_peer',side_effect=push):e.tick()
                        op=e.operation(a['operation_id'],'same-owner')
                        self.assertEqual(op['status'],'running')
                        r=running.call('reply',id=op['backend_id'],body='answer',key='reply-'+str(n))
                        e.tick()
                        self.assertEqual(e.operation(a['operation_id'],'same-owner')['status'],'succeeded')
                        if ledgers==1:self.assertEqual(running.call('show',id=r['id'])['state'],'done')
            finally:
                for e in engines:e.close()

    def test_consumed_replies_do_not_exhaust_small_broker(self):
        self.exercise(5,1,3)

    def test_two_ledgers_reusing_client_key_do_not_collide(self):
        self.exercise(2,2,20)

if __name__=='__main__': unittest.main()
