"""MCP tests use disposable brokers and fake providers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import os
import subprocess
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
import bridge
import broker
from support import RunningBroker
from test_codex_live import FakeDesktop
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
        backend.verify_routes=lambda s, **options:None
        keys=[]
        def rpc(name,**kw):
            keys.append(kw['key'])
            return {'route':{'session':session['uuid'],'transport':'claude-peer','cwd':str(self.root)}}
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

    def route_checked_owned_session(self, key='route_checked_create_01'):
        self.engine.backend = self.backend  # Creation stays on the synthetic backend.
        self.config['workspaces']['test']['agent_type'] = 'agy'
        sid = self.create(key)['agent_session_id']
        session = self.engine.session(sid, 'owner-a')
        backend = phase.Backend('/unused-synthetic-broker')
        routes = [dict(project=session['workspace']['project'], name=session['route'],
                       cwd=str(self.root), transport='agy', session=session['uuid']),
                  dict(project=session['workspace']['project'], name=session['sender'],
                       cwd=str(self.root), transport='realtime', session=None)]
        calls = []
        def rpc(op, **fields):
            calls.append((op, fields))
            if op == 'ping':
                return {'instance': self.backend.current}
            if op == 'routes':
                return [dict(row) for row in routes]
            if op == 'send':
                return dict(id=str(uuid.uuid4()), state='pending', route=dict(routes[0]), return_route=dict(routes[1]))
            raise AssertionError('Unexpected synthetic broker mutation: ' + op)
        backend.rpc = rpc
        self.engine.backend = backend
        return sid, session, backend, routes, calls

    def test_owned_live_agy_route_collision_is_visible_before_accept_without_operation(self):
        sid, session, backend, routes, calls = self.route_checked_owned_session()
        other = str(uuid.uuid4())
        routes[0]['session'] = other
        live = {value: {'id': value, 'cwd': str(self.root), 'status': 'idle'} for value in (session['uuid'], other)}
        before = len(self.engine.rows())
        with patch.object(phase.sessions, 'resolve_agy_peer', side_effect=lambda value: live[value]), patch.object(backend, 'push') as push:
            result = self.send(sid)
        self.assertEqual((result['error']['code'], result['error']['message']),
                         ('BACKEND_UNAVAILABLE', phase.ROUTE_CHANGED_MESSAGE))
        self.assertEqual(len(self.engine.rows()), before)
        self.assertEqual(routes[0]['session'], other)  # Neither live session was replaced.
        self.assertFalse(any(op in ('send', 'register') for op, _ in calls))
        push.assert_not_called()

    def test_route_change_after_accept_fails_before_post_and_does_not_leave_busy(self):
        for boundary in ('preflight', 'post'):
            with self.subTest(boundary=boundary):
                sid, session, backend, routes, calls = self.route_checked_owned_session('route_create_' + boundary)
                original = session['uuid']
                with patch.object(phase.sessions, 'resolve_agy_peer', return_value={'id': original, 'cwd': str(self.root), 'status': 'idle'}), patch.object(backend, 'push') as push:
                    accepted = self.send(sid, key='route_message_' + boundary)['data']
                    if boundary == 'preflight':
                        routes[0]['session'] = str(uuid.uuid4())
                    else:
                        check = backend.require_send_availability
                        def changed_after_preflight(value):
                            check(value)
                            routes[0]['session'] = str(uuid.uuid4())
                        backend.require_send_availability = changed_after_preflight
                    self.engine.tick()
                    operation = self.call('bridge_operation_get', operation_id=accepted['operation_id'])['data']
                    self.assertEqual((operation['status'], operation['failure']['code'], operation['failure']['message']),
                                     ('failed', 'BACKEND_UNAVAILABLE', phase.ROUTE_CHANGED_MESSAGE))
                    self.assertIsNone(self.engine.operation(accepted['operation_id'], 'owner-a')['backend_id'])
                    self.assertFalse(any(op in ('send', 'register') for op, _ in calls))
                    push.assert_not_called()
                    # An explicit operator repair permits a separately keyed send;
                    # replay of the failed operation never creates another intent.
                    routes[0]['session'] = original
                    backend.require_send_availability = phase.Backend.require_send_availability.__get__(backend)
                    self.assertTrue(self.send(sid, key='route_message_' + boundary)['data']['replayed'])
                    self.assertTrue(self.send(sid, key='after_route_fix_' + boundary)['ok'])
                    self.engine.fail(self.engine.rows()[-1], 'BACKEND_UNAVAILABLE', False)

    def test_route_preflight_allows_absent_discovery_bindings_and_pinned_sender_only(self):
        sid, session, backend, routes, calls = self.route_checked_owned_session()
        live = {'id': session['uuid'], 'cwd': str(self.root), 'status': 'idle'}
        target = dict(routes[0])
        with patch.object(phase.sessions, 'resolve_agy_peer', return_value=live):
            routes.clear()
            self.assertEqual(backend.availability(dict(session, discovered=True, registered=False)), 'ready')
            with self.assertRaises(phase.RouteBindingRefusal):
                backend.availability(session)  # Existing owned binding cannot disappear silently.
            routes.append(target)
            self.assertEqual(backend.availability(dict(session, pinned=True, explicit_type=True)), 'ready')
            routes[0]['session'] = str(uuid.uuid4())
            with self.assertRaises(phase.RouteBindingRefusal):
                backend.availability(dict(session, discovered=True, registered=False))
        self.assertFalse(any(op in ('send', 'register') for op, _ in calls))

    def test_known_sender_collision_is_checked_before_creating_discovered_target(self):
        sid, session, backend, routes, calls = self.route_checked_owned_session()
        routes[:] = [dict(routes[1], transport='agy', session=str(uuid.uuid4()))]
        with patch.object(phase.sessions, 'resolve_agy_peer', return_value={'id': session['uuid'], 'cwd': str(self.root), 'status': 'idle'}):
            with self.assertRaises(phase.RouteBindingRefusal):
                backend.register(dict(session, discovered=True, registered=False))
        self.assertFalse(any(op in ('send', 'register') for op, _ in calls))

    def test_authoritative_register_race_refuses_without_post_or_push(self):
        cases = (('target', ValueError(phase.REGISTER_CONFLICT_ERROR), False),
                 ('sender', ValueError(phase.REGISTER_CONFLICT_ERROR), False),
                 ('target', TimeoutError(phase.REGISTER_CONFLICT_ERROR), True),
                 ('target', ValueError(phase.REGISTER_CONFLICT_ERROR + ' detail'), True))
        for index, (binding, error, ambiguous) in enumerate(cases):
            with self.subTest(binding=binding, error=type(error).__name__, ambiguous=ambiguous):
                sid, session, backend, routes, calls = self.route_checked_owned_session('register_race_create_%d' % index)
                session['discovered'] = True
                self.engine.db.execute('UPDATE owned_sessions SET doc=? WHERE id=?', (json.dumps(session), sid))
                original_routes = [dict(row) for row in routes]
                rpc, post = backend.rpc, backend.post
                missing = session['route'] if binding == 'target' else session['sender']
                def post_after_binding_disappears(operation, value):
                    routes[:] = [row for row in routes if row['name'] != missing]
                    return post(operation, value)
                def competing_register(op, **fields):
                    if op == 'register':
                        calls.append((op, fields))
                        routes.append(dict(fields, transport='agy', session=str(uuid.uuid4())))
                        raise error
                    return rpc(op, **fields)
                backend.rpc = competing_register
                with patch.object(phase.sessions, 'resolve_agy_peer', return_value={'id': session['uuid'], 'cwd': str(self.root), 'status': 'idle'}), patch.object(backend, 'post', side_effect=post_after_binding_disappears), patch.object(backend, 'push') as push:
                    accepted = self.send(sid, key='register_race_message_%d' % index)['data']
                    self.engine.tick()
                    self.engine.tick()
                    operation = self.call('bridge_operation_get', operation_id=accepted['operation_id'])['data']
                    self.assertEqual((operation['status'], operation['failure']['code']),
                                     ('unknown', 'DELIVERY_UNKNOWN') if ambiguous else ('failed', 'BACKEND_UNAVAILABLE'))
                    if not ambiguous:
                        self.assertEqual(operation['failure']['message'], phase.ROUTE_CHANGED_MESSAGE)
                    self.assertIsNone(self.engine.operation(accepted['operation_id'], 'owner-a')['backend_id'])
                    self.assertEqual(sum(op == 'register' for op, _ in calls), 1)
                    self.assertFalse(any(op == 'send' for op, _ in calls))
                    push.assert_not_called()
                    self.assertTrue(self.send(sid, key='register_race_message_%d' % index)['data']['replayed'])
                    routes[:] = original_routes  # Independent repair; the operation is never replayed.
                    next_send = self.send(sid, key='after_register_race_%d' % index)
                    if ambiguous:
                        self.assertEqual(next_send['error']['code'], 'SESSION_BUSY')
                    else:
                        self.assertTrue(next_send['ok'])
                        self.engine.fail(self.engine.operation(next_send['data']['operation_id'], 'owner-a'), 'BACKEND_UNAVAILABLE', False)

    def test_post_response_route_race_remains_unknown_without_push_or_route_mutation(self):
        sid, session, backend, routes, calls = self.route_checked_owned_session()
        with patch.object(phase.sessions, 'resolve_agy_peer', return_value={'id': session['uuid'], 'cwd': str(self.root), 'status': 'idle'}), patch.object(backend, 'push') as push:
            accepted = self.send(sid)['data']
            rpc = backend.rpc
            def change_at_send(op, **fields):
                if op == 'send':
                    routes[0]['session'] = str(uuid.uuid4())
                return rpc(op, **fields)
            backend.rpc = change_at_send
            self.engine.tick()
            operation = self.call('bridge_operation_get', operation_id=accepted['operation_id'])['data']
            self.assertEqual((operation['status'], operation['failure']['code']), ('unknown', 'DELIVERY_UNKNOWN'))
            self.assertEqual(sum(op == 'send' for op, _ in calls), 1)
            self.assertFalse(any(op == 'register' for op, _ in calls))
            push.assert_not_called()
            self.assertEqual(self.send(sid, key='race_still_busy_001')['error']['code'], 'SESSION_BUSY')

    def test_typed_route_check_after_post_stays_ambiguous_and_is_not_retried(self):
        sid = self.create()['agent_session_id']
        accepted = self.send(sid)['data']
        with patch.object(self.backend, 'push', side_effect=phase.RouteBindingRefusal('Session identity changed')) as push:
            self.engine.tick()
            self.engine.tick()
            self.assertEqual(push.call_count, 1)
        operation = self.call('bridge_operation_get', operation_id=accepted['operation_id'])['data']
        self.assertEqual((operation['status'], operation['failure']['code']), ('unknown', 'DELIVERY_UNKNOWN'))
        self.assertEqual(self.backend.posts, 1)
        self.assertIsNotNone(self.engine.operation(accepted['operation_id'], 'owner-a')['backend_id'])
        self.assertEqual(self.send(sid, key='after_post_still_busy')['error']['code'], 'SESSION_BUSY')

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
        self.assertTrue(self.send(sid,key='other_message_0001')['ok'])

    def test_distinct_keys_queue_fifo_and_keep_independent_out_of_order_replies(self):
        sid=self.create()['agent_session_id']
        submitted=[]
        original=self.backend.post
        def post(op, session):
            submitted.append(op['message'])
            return original(op, session)
        self.backend.post=post
        accepted=[self.send(sid,key='queued_message_%04d'%i,text='message %d'%i)['data'] for i in range(3)]
        self.engine.tick()
        self.assertEqual(submitted,['message 0','message 1','message 2'])
        ops=[self.engine.operation(a['operation_id'],'owner-a') for a in accepted]
        self.assertEqual(len({o['backend_id'] for o in ops}),3)
        self.assertTrue(all(o['stage']=='observing' and o['status']=='running' for o in ops))
        for i in (2,0,1):
            op=ops[i]
            self.backend.replies[op['backend_id']]=[
                {'id':'wrong','reply_to':ops[(i+1)%3]['backend_id'],'created':0,'body':'wrong answer'},
                {'id':'reply%d'%i,'reply_to':op['backend_id'],'created':1,'body':'answer %d'%i}]
            self.engine.tick()
            self.assertEqual(self.engine.get({'operation_id':op['operation_id']},'owner-a')['result']['text'],'answer %d'%i)
        self.assertEqual((self.backend.posts,self.backend.pushes),(3,3))

    def test_concurrent_distinct_keys_and_replay_at_global_capacity(self):
        sid=self.create()['agent_session_id']
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda i:self.send(sid,key='concurrent_send_%04d'%i),range(64)))
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(len({r['data']['operation_id'] for r in results}),64)
        self.assertEqual(self.send(sid,key='over_capacity_00001')['error']['code'],'CAPACITY_EXCEEDED')
        self.assertTrue(self.send(sid,key='concurrent_send_0000')['data']['replayed'])
        self.assertEqual(self.send(sid,key='concurrent_send_0000',text='changed')['error']['code'],'IDEMPOTENCY_CONFLICT')
        self.assertEqual((self.backend.posts,self.backend.pushes),(0,0))

    def test_accept_during_push_queues_until_serial_dispatch_returns(self):
        sid=self.create()['agent_session_id']; entered=threading.Event(); release=threading.Event()
        original=self.backend.push
        def push(message, session):
            entered.set()
            if not release.wait(3): raise RuntimeError('test did not release push')
            return original(message, session)
        self.backend.push=push
        first=self.send(sid)['data']
        thread=threading.Thread(target=self.engine.tick); thread.start()
        try:
            self.assertTrue(entered.wait(3))
            second=self.send(sid,key='during_push_000001')['data']
            self.assertEqual(self.engine.operation(first['operation_id'],'owner-a')['stage'],'push_intent')
            self.assertEqual(self.engine.operation(second['operation_id'],'owner-a')['stage'],'queued')
            self.assertEqual((self.backend.posts,self.backend.pushes),(1,0))
        finally:
            release.set(); thread.join(3)
        self.engine.tick()
        self.assertEqual((self.backend.posts,self.backend.pushes),(2,2))

    def test_busy_created_and_pinned_sessions_accept_all_provider_types(self):
        for agent_type in phase.AGENT_TYPES:
            with self.subTest(agent_type=agent_type):
                self.config['workspaces']['test']['agent_type']=agent_type
                sid=self.create('create_busy_'+agent_type+'_001')['agent_session_id']
                self.backend.availability=lambda s:'busy'
                self.assertTrue(self.send(sid,key='busy_created_'+agent_type)['ok'])
                self.engine.tick()
                pinned=self.grant()
                self.config['pinned_sessions']['hq']['agent_type']=agent_type
                pinned=next(iter(phase.pinned_sessions(self.config)))
                self.assertTrue(self.send(pinned,key='busy_pinned_'+agent_type+'_001')['ok'])
                self.engine.tick()
        self.assertEqual((self.backend.posts,self.backend.pushes),(6,6))

    def test_unknown_quarantines_queued_alias_until_read_only_reply_settles(self):
        sid=self.grant()
        grant=dict(self.config['pinned_sessions']['hq'],route='alias-route')
        self.config['pinned_sessions']['alias']=grant
        alias=next(s for s in phase.pinned_sessions(self.config) if s!=sid)
        first=self.send(sid)['data']; second=self.send(alias,key='alias_queued_00001')['data']
        self.backend.crash='push'; self.engine.tick()
        first_op=self.engine.operation(first['operation_id'],'owner-a')
        self.assertEqual(first_op['status'],'unknown')
        self.assertEqual(self.engine.operation(second['operation_id'],'owner-a')['stage'],'queued')
        self.assertEqual((self.backend.posts,self.backend.pushes),(1,1))
        self.assertEqual(self.send(alias,key='alias_blocked_0001')['error']['code'],'SESSION_BUSY')
        self.assertTrue(self.send(sid)['data']['replayed'])
        self.assertEqual(self.send(sid,text='changed')['error']['code'],'IDEMPOTENCY_CONFLICT')
        self.restart(); self.engine.tick()
        self.assertEqual((self.backend.posts,self.backend.pushes),(1,1))
        self.backend.replies[first_op['backend_id']]=[{'id':'late','reply_to':first_op['backend_id'],'created':1,'body':'delivered'}]
        self.backend.crash=None; self.engine.watch_at.clear(); self.engine.tick()
        self.assertEqual(self.engine.operation(first['operation_id'],'owner-a')['status'],'succeeded')
        self.assertEqual(self.engine.operation(second['operation_id'],'owner-a')['stage'],'observing')
        self.assertEqual((self.backend.posts,self.backend.pushes),(2,2))

    def test_restart_queued_messages_preserves_fifo_without_repeating_dispatch(self):
        sid=self.create()['agent_session_id']
        first=self.send(sid)['data']; self.engine.tick()
        second=self.send(sid,key='restart_queued_0001')['data']
        self.restart(); self.engine.tick(); self.restart(); self.engine.tick()
        self.assertEqual((self.backend.posts,self.backend.pushes),(2,2))
        self.assertEqual(self.engine.operation(first['operation_id'],'owner-a')['stage'],'observing')
        self.assertEqual(self.engine.operation(second['operation_id'],'owner-a')['stage'],'observing')

    def test_unknown_result_allows_new_message_without_resending_old_message(self):
        sid=self.create()['agent_session_id']; first=self.send(sid)['data']; self.engine.tick()
        op=self.engine.operation(first['operation_id'],'owner-a')
        self.engine.fail(op)
        self.assertTrue(self.send(sid,key='unknown_result_new1')['ok'])
        self.engine.tick(); self.restart(); self.engine.tick()
        self.assertEqual(self.engine.operation(first['operation_id'],'owner-a')['status'],'unknown')
        self.assertEqual((self.backend.posts,self.backend.pushes),(2,2))

    def test_unsafe_unknown_classification_and_transport_identity(self):
        sid=self.grant(); accepted=self.send(sid)['data']
        op=self.engine.operation(accepted['operation_id'],'owner-a')
        destination=op['destination']
        for stage, state, backend_id in (
                ('post_intent',None,None),('posted','pending','known'),
                ('push_intent','submitted','known'),('observing','uncertain','known'),
                ('observing','dispatching','known'),('observing','submitted',None)):
            with self.subTest(stage=stage,state=state,backend_id=backend_id):
                self.engine.update(op,status='unknown',stage=stage,backend_message_state=state,backend_id=backend_id)
                self.assertEqual(self.send(sid,key='unsafe_classification')['error']['code'],'SESSION_BUSY')
        # Provider identities remain distinct even when UUID strings coincide.
        self.config['pinned_sessions']['other']=dict(self.config['pinned_sessions']['hq'],
            route='other-provider',agent_type='agy')
        other=next(s for s in phase.pinned_sessions(self.config) if s!=sid)
        self.assertTrue(self.send(other,key='different_transport')['ok'])
        # A current grant cannot prove a legacy write's original transport.
        op.pop('destination'); self.engine.update(op)
        self.assertIsNone(self.engine.operation_destination(op))
        self.assertEqual(self.send(sid,key='legacy_quarantine_1')['error']['code'],'SESSION_BUSY')

    def test_effective_transport_is_snapshotted_and_retargeting_refuses_dispatch(self):
        sid=self.grant()
        transport=['agy']
        self.backend.session_transport=lambda session:transport[0]
        first=self.send(sid)['data']
        op=self.engine.operation(first['operation_id'],'owner-a')
        self.assertEqual(op['destination'],{'uuid':self.config['pinned_sessions']['hq']['uuid'],'transport':'agy'})
        transport[0]='claude-peer'
        self.engine.tick()
        saved=self.engine.operation(first['operation_id'],'owner-a')
        self.assertEqual((saved['status'],saved['failure']['code']),('failed','BACKEND_UNAVAILABLE'))
        self.assertEqual((self.backend.posts,self.backend.pushes),(0,0))

    def test_legacy_unsafe_identity_cannot_follow_current_transport_past_alias_quarantine(self):
        sid=self.grant(); current=['agy']
        self.backend.session_transport=lambda s:s['transport'] if s.get('explicit_type') else current[0]
        accepted=self.send(sid)['data']; op=self.engine.operation(accepted['operation_id'],'owner-a')
        op.pop('destination'); self.engine.update(op,status='unknown',stage='post_intent')
        current[0]='claude-peer'
        self.config['pinned_sessions']['original']=dict(self.config['pinned_sessions']['hq'],
            route='original-transport',agent_type='agy')
        alias=next(s for s in phase.pinned_sessions(self.config) if s!=sid)
        self.assertEqual(self.send(alias,key='legacy_original_alias')['error']['code'],'SESSION_BUSY')
        # Result uncertainty is independent of identity recovery: no old write is retried.
        self.engine.update(op,stage='observing',backend_id='known',backend_message_state='submitted')
        self.assertTrue(self.send(alias,key='legacy_result_alias')['ok'])
    def test_definite_broker_capacity_refusal_fails_without_push_and_releases_target(self):
        for index, reason in enumerate(sorted(phase.MESSAGE_CAPACITY_ERRORS)):
            with self.subTest(reason=reason):
                sid = self.create('create_capacity_%02d' % index)['agent_session_id']
                oid = self.send(sid, key='capacity_message_%02d' % index)['data']['operation_id']
                pushes = self.backend.pushes
                with patch.object(self.backend, 'post', side_effect=ValueError(reason)) as post:
                    self.engine.tick()
                    self.assertEqual(post.call_count, 1)
                operation = self.call('bridge_operation_get', operation_id=oid)['data']
                self.assertEqual(operation['status'], 'failed')
                self.assertEqual(operation['failure']['code'], 'BACKEND_UNAVAILABLE')
                self.assertEqual(operation['failure']['message'], phase.CAPACITY_FAILURE_MESSAGE)
                self.assertEqual(self.backend.pushes, pushes)
                self.assertIsNone(self.engine.operation(oid, 'owner-a')['backend_id'])
                replay = self.send(sid, key='capacity_message_%02d' % index)
                self.assertTrue(replay['data']['replayed'])
                self.engine.tick()
                self.assertEqual(self.backend.pushes, pushes)
                self.assertTrue(self.send(sid, key='after_capacity_%02d' % index)['ok'])
                self.engine.tick()
                self.assertEqual(self.backend.pushes, pushes + 1)


    def test_ambiguous_post_errors_remain_unknown_without_retry_and_hold_target(self):
        errors = (TimeoutError('response lost'), OSError('disconnected'),
                  ValueError('unrecognized post response'), ValueError(phase.REGISTER_CONFLICT_ERROR))
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                sid = self.create('create_ambiguous_%02d' % index)['agent_session_id']
                oid = self.send(sid, key='ambiguous_message_%02d' % index)['data']['operation_id']
                pushes = self.backend.pushes
                with patch.object(self.backend, 'post', side_effect=error) as post:
                    self.engine.tick()
                    self.engine.tick()
                    self.assertEqual(post.call_count, 1)
                operation = self.call('bridge_operation_get', operation_id=oid)['data']
                self.assertEqual(operation['status'], 'unknown')
                self.assertEqual(operation['failure']['code'], 'DELIVERY_UNKNOWN')
                self.assertEqual(self.backend.pushes, pushes)
                self.assertEqual(self.send(sid, key='after_ambiguous_%02d' % index)['error']['code'], 'SESSION_BUSY')


    def test_answered_without_retained_reply_releases_target_and_late_watch_settles(self):
        sid, oid, mid = self.observing()
        self.backend.messages[mid]['state'] = 'answered'
        self.engine.tick()
        op = self.call('bridge_operation_get', operation_id=oid)['data']
        self.assertEqual((op['status'], op['failure']['code'], op['failure']['message']),
                         ('failed', 'BACKEND_UNAVAILABLE', phase.REPLY_NOT_RETAINED_MESSAGE))
        self.assertTrue(self.send(sid, key='after_lost_reply_001')['ok'])
        # A later reply is observed without replaying the original post/push.
        self.reply(mid, 'retained later answer')
        self.engine.tick()
        result = self.call('bridge_operation_get', operation_id=oid)['data']
        self.assertEqual((result['status'], result['result']['text']), ('succeeded', 'retained later answer'))
        self.assertEqual(self.engine.operation(oid, 'owner-a')['settled_after'],
                         {'status': 'failed', 'code': 'BACKEND_UNAVAILABLE'})
        self.assertEqual((self.backend.posts, self.backend.pushes), (2, 2))  # New explicit operation only.


    def test_exact_missing_delivered_message_releases_target_without_replay(self):
        sid, oid, mid = self.observing()
        with patch.object(self.backend, 'observe', side_effect=ValueError(phase.UNKNOWN_MESSAGE)):
            self.engine.tick()
            self.engine.tick()
        operation = self.call('bridge_operation_get', operation_id=oid)['data']
        self.assertEqual((operation['status'], operation['failure']['code'], operation['failure']['message']),
                         ('failed', 'BACKEND_UNAVAILABLE', phase.MESSAGE_NOT_RETAINED_MESSAGE))
        self.assertEqual((self.backend.posts, self.backend.pushes), (1, 1))
        self.assertTrue(self.send(sid, key='after_lost_message_1')['ok'])


    def test_missing_message_epoch_race_or_unreadable_recheck_remains_unknown(self):
        for index, changed in enumerate(('new-instance', TimeoutError('epoch unavailable'))):
            with self.subTest(changed=type(changed).__name__):
                sid, oid, mid = self.observing('missing_epoch_%02d' % index)
                with patch.object(self.backend, 'instance', side_effect=[self.backend.current, changed]), patch.object(self.backend, 'observe', side_effect=ValueError(phase.UNKNOWN_MESSAGE)):
                    self.engine.tick()
                operation = self.call('bridge_operation_get', operation_id=oid)['data']
                self.assertEqual((operation['status'], operation['failure']['code']), ('unknown', 'DELIVERY_UNKNOWN'))
                self.assertFalse(self.engine.blocked([self.engine.operation(oid, 'owner-a')],
                                                     self.engine.destination(self.engine.session(sid, 'owner-a'))))
        self.assertEqual((self.backend.posts, self.backend.pushes), (2, 2))


    def test_wrong_answered_parent_is_not_classified_as_missing_reply(self):
        sid, oid, mid = self.observing()
        self.engine.observe_retry_limit = 1
        with patch.object(self.backend, 'observe', return_value=({'id': 'wrong-parent', 'state': 'answered'}, [])):
            self.engine.tick()
        operation = self.call('bridge_operation_get', operation_id=oid)['data']
        self.assertEqual((operation['status'], operation['failure']['code']), ('unknown', 'DELIVERY_UNKNOWN'))
        self.assertEqual((self.backend.posts, self.backend.pushes), (1, 1))


    def test_missing_message_without_confirmed_delivery_remains_ambiguous(self):
        sid, oid, mid = self.observing()
        op = self.engine.operation(oid, 'owner-a')
        self.engine.update(op, stage='posted', backend_message_state='pending')
        self.engine.observe_retry_limit = 1
        with patch.object(self.backend, 'observe', side_effect=ValueError(phase.UNKNOWN_MESSAGE)):
            self.engine.tick()
        operation = self.call('bridge_operation_get', operation_id=oid)['data']
        self.assertEqual((operation['status'], operation['failure']['code']), ('unknown', 'DELIVERY_UNKNOWN'))
        self.assertEqual(self.send(sid, key='still_ambiguous_001')['error']['code'], 'SESSION_BUSY')
        self.assertEqual((self.backend.posts, self.backend.pushes), (1, 1))


    def test_exact_capacity_wire_matches_legacy_embedded_broker_without_new_import(self):
        self.assertEqual(phase.MESSAGE_CAPACITY_ERRORS, broker.MESSAGE_CAPACITY_ERRORS)
        state = broker.State(max_messages=1)
        for name in ('alpha', 'beta'):
            state.action(dict(op='register', project='synthetic', name=name, cwd='/tmp'))
        args = dict(op='send', project='synthetic', sender='alpha', recipient='beta', body='synthetic', key='first')
        state.action(args)
        with self.assertRaises(ValueError) as caught:
            state.action(dict(args, key='second'))
        self.assertIn(str(caught.exception), phase.MESSAGE_CAPACITY_ERRORS)
        self.assertEqual(len(phase.MESSAGE_CAPACITY_ERRORS), 2)
        self.assertIn('Active-message limit reached: TTL or protected work prevents safe cleanup; inspect retention --live; no messages evicted before TTL', phase.MESSAGE_CAPACITY_ERRORS)

    def test_late_retention_result_preserves_event_parent_and_committed_notification(self):
        sid, oid, mid = self.observing()
        self.backend.messages[mid]['state'] = 'answered'
        with patch.object(self.engine, 'events') as events:
            self.engine.tick()
            self.assertEqual(self.engine.operation(oid, 'owner-a')['status'], 'failed')
            events.result_ready.assert_not_called()
            self.reply(mid, 'retained late result')
            self.engine.tick()
            events.result_ready.assert_called_once()
            operation, parent, replies = events.result_ready.call_args.args
            self.assertEqual(parent['id'], mid)
            self.assertEqual(replies[0]['body'], 'retained late result')
            self.assertEqual(operation['status'], 'succeeded')
            self.assertEqual(self.engine.operation(oid, 'owner-a')['status'], 'succeeded')
        self.assertEqual((self.backend.posts, self.backend.pushes), (1, 1))

    def test_post_push_reply_and_immutable_pagination(self):
        sid=self.create()['agent_session_id']; accepted=self.send(sid,text='  exact 한글  ')['data']
        self.engine.tick(); self.assertEqual((self.backend.posts,self.backend.pushes),(1,1))
        oid=accepted['operation_id']; op=self.engine.operation(oid,'owner-a')
        self.assertEqual(op['message'],'  exact 한글  ')
        self.assertEqual(self.call('bridge_operation_get',operation_id=oid)['data']['status'],'running')
        self.backend.messages[op['backend_id']]['state']='answered'; self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'failed')
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
        self.assertEqual(self.call('bridge_session_create',workspace_alias='test',idempotency_key='valid_key_0000001',model='x')['error']['code'],'UNSUPPORTED_MODEL_SELECTION')
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
    def test_closing_route_refuses_post_without_unknown_or_push(self):
        self.assertEqual(phase.CLOSING_ROUTE_ERROR, broker.CLOSING_ROUTE_ERROR)
        sid = self.create()['agent_session_id']
        key = 'closing_route_message'
        oid = self.send(sid, key=key)['data']['operation_id']
        with patch.object(self.backend, 'post', side_effect=ValueError(broker.CLOSING_ROUTE_ERROR)) as post:
            self.engine.tick()
            self.assertEqual(post.call_count, 1)
        operation = self.call('bridge_operation_get', operation_id=oid)['data']
        self.assertEqual((operation['status'], operation['failure']['code']), ('failed', 'BACKEND_UNAVAILABLE'))
        self.assertEqual(operation['failure']['message'], broker.CLOSING_ROUTE_ERROR)
        self.assertEqual(self.backend.pushes, 0)
        self.assertTrue(self.send(sid, key=key)['data']['replayed'])
        self.assertTrue(self.send(sid, key='after_closing_route_01')['ok'])

    def test_codex_owner_discovery_timeout_closes_mcp_operation_with_pending_message(self):
        sid = self.create()['agent_session_id']
        key = 'owner_discovery_timeout'
        oid = self.send(sid, key=key)['data']['operation_id']
        detail = phase.codex_live.OwnerUnavailable.TIMEOUT + phase.codex_live.OwnerUnavailable.WORKSPACE_MISSING

        def reject(message, session):
            self.backend.pushes += 1
            pending = dict(message, state='pending', note=detail)
            self.backend.messages[message['id']] = pending
            raise phase.delivery.PushFailed(detail, pending)

        with patch.object(self.backend, 'push', side_effect=reject):
            self.engine.tick()
        self.restart()
        operation = self.call('bridge_operation_get', operation_id=oid)['data']
        self.assertEqual((operation['status'], operation['backend_message_state']), ('failed', 'pending'))
        self.assertEqual(operation['failure']['code'], 'BACKEND_UNAVAILABLE')
        self.assertEqual(operation['failure']['message'], detail)
        self.assertTrue(self.send(sid, key=key)['data']['replayed'])
        self.engine.tick()
        self.assertEqual((self.backend.posts, self.backend.pushes), (1, 1))
        self.assertTrue(self.send(sid, key='fresh_after_owner_timeout')['ok'])

    def test_archived_push_and_observation_fail_definitely_without_replay(self):
        for mode in ('push', 'observe'):
            with self.subTest(mode=mode):
                sid = self.create('archive_create_' + mode)['agent_session_id']
                key = 'archive_message_' + mode
                oid = self.send(sid, key=key)['data']['operation_id']

                def reject(message, session):
                    self.backend.pushes += 1
                    failure = dict(message, state='failed', reason=phase.delivery.ARCHIVED_REASON)
                    self.backend.messages[message['id']] = failure
                    raise phase.delivery.PushFailed('archived', failure)

                if mode == 'push':
                    with patch.object(self.backend, 'push', side_effect=reject):
                        self.engine.tick()
                else:
                    self.engine.tick()
                    mid = self.engine.operation(oid, 'owner-a')['backend_id']
                    self.backend.messages[mid].update(state='failed', reason=phase.delivery.ARCHIVED_REASON)
                    self.engine.tick()
                self.restart()
                result = self.call('bridge_operation_get', operation_id=oid)['data']
                self.assertEqual((result['status'], result['backend_message_state']), ('failed', 'failed'))
                self.assertEqual(result['failure']['code'], 'BACKEND_UNAVAILABLE')
                self.assertEqual(result['failure']['message'], phase.codex_live.ThreadArchived.MESSAGE)
                counts = (self.backend.posts, self.backend.pushes)
                self.assertTrue(self.send(sid, key=key)['data']['replayed'])
                self.engine.tick()
                self.assertEqual((self.backend.posts, self.backend.pushes), counts)
                self.assertTrue(self.send(sid, key='fresh_after_archive_' + mode)['ok'])
                self.engine.fail(self.engine.rows()[-1], 'BACKEND_UNAVAILABLE', unknown=False)

    def test_recovery_observes_only_and_broker_loss_does_not_replay_delivered_message(self):
        sid=self.create()['agent_session_id']; oid=self.send(sid)['data']['operation_id']; self.engine.tick()
        self.restart(); self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'running')
        self.assertEqual(self.backend.pushes,1)
        self.backend.current='restarted'; self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'unknown')
        self.assertTrue(self.send(sid,key='new_message_00001')['ok'])
    def test_posted_restart_is_unknown_not_pushed(self):
        sid=self.create()['agent_session_id']; oid=self.send(sid)['data']['operation_id']
        op=self.engine.operation(oid,'owner-a'); m=self.backend.post(op,{})
        self.engine.update(op,status='running',stage='posted',backend_id=m['id'],instance=self.backend.current)
        self.restart(); self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'unknown')
        self.assertEqual(self.backend.pushes,0)

    def test_restart_with_existing_reply_preserves_committed_success(self):
        sid=self.create()['agent_session_id']; oid=self.send(sid)['data']['operation_id']; self.engine.tick()
        op=self.engine.operation(oid,'owner-a')
        self.backend.replies[op['backend_id']]=[{'id':'restart-answer','reply_to':op['backend_id'],
            'created':'1','body':'reply present before facade restart'}]
        self.restart()
        result=self.engine.get({'operation_id':oid},'owner-a')
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['result']['text'],'reply present before facade restart')
        self.assertEqual(self.backend.pushes,1)
    def test_evicted_message_unknown(self):
        sid=self.create()['agent_session_id']; oid=self.send(sid)['data']['operation_id']; self.engine.tick()
        self.engine.observe_retry_base=0; self.backend.messages.clear(); self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'running')
        for _ in range(phase.OBSERVE_RETRY_LIMIT-1): self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'unknown')

    # Issue #134: a failed read of a known message is not a delivery outcome.
    def observing(self,key='message_key_000001'):
        sid=self.create('create_'+key)['agent_session_id']; oid=self.send(sid,key=key)['data']['operation_id']
        self.engine.tick(); op=self.engine.operation(oid,'owner-a')
        self.assertEqual((op['status'],op['stage'],op['backend_message_state']),('running','observing','submitted'))
        self.retired=[]; self.backend.retire_reply=lambda mid,parent:self.retired.append((mid,parent))
        return sid,oid,op['backend_id']
    def flaky(self,times,error=None):
        real=self.backend.observe; left=[times]; calls=[]
        def observe(mid):
            calls.append(mid)
            if left[0]>0:
                left[0]-=1; raise error or TimeoutError('timed out')
            return real(mid)
        self.backend.observe=observe
        return calls
    def reply(self,mid,body='the answer',rid='r1'):
        self.backend.replies.setdefault(mid,[]).append({'id':rid,'reply_to':mid,'created':1,'body':body})

    def test_one_failed_observe_keeps_running_then_reply_succeeds(self):
        sid,oid,mid=self.observing(); self.engine.observe_retry_base=0
        self.flaky(1); self.engine.tick()
        op=self.engine.operation(oid,'owner-a')
        self.assertEqual((op['status'],op['failure'],op['observe_errors']),('running',None,1))
        self.assertEqual((op['error']['type'],op['error']['message']),('TimeoutError','timed out'))
        self.assertEqual(self.call('bridge_operation_get',operation_id=oid)['data']['status'],'running')
        self.reply(mid); self.engine.tick()
        got=self.call('bridge_operation_get',operation_id=oid)['data']
        self.assertEqual((got['status'],got['result']['text'],got['failure']),('succeeded','the answer',None))
        self.assertEqual(self.retired,[('r1',mid)])
        self.assertEqual(self.engine.operation(oid,'owner-a')['observe_errors'],0)
        self.assertEqual((self.backend.posts,self.backend.pushes),(1,1))
        self.assertTrue(self.send(sid,key='next_message_0001')['ok'])

    def test_failed_observe_waits_before_the_next_read(self):
        sid,oid,mid=self.observing(); calls=self.flaky(1)
        self.engine.tick(); self.engine.tick(); self.engine.tick()
        self.assertEqual(len(calls),1)
        self.engine.observe_at[oid]=0; self.engine.tick()
        self.assertEqual(len(calls),2)

    def test_backend_failed_or_uncertain_still_fails(self):
        for state,status,code in (('failed','failed','BACKEND_UNAVAILABLE'),('uncertain','unknown','DELIVERY_UNKNOWN')):
            with self.subTest(state=state):
                sid,oid,mid=self.observing('terminal_'+state+'_00001')
                self.backend.messages[mid]['state']=state; self.engine.tick()
                got=self.call('bridge_operation_get',operation_id=oid)['data']
                self.assertEqual((got['status'],got['failure']['code'],got['result']),(status,code,None))
                self.assertNotIn('error',self.engine.operation(oid,'owner-a'))

    def test_instance_change_still_fails_and_ends_the_watch(self):
        sid,oid,mid=self.observing(); self.backend.current='restarted'; self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'unknown')
        self.reply(mid); self.engine.tick()
        op=self.engine.operation(oid,'owner-a')
        self.assertEqual((op['status'],op['reply_watch_closed']),('unknown','instance_changed'))
        calls=self.flaky(0); self.engine.watch_at.clear(); self.engine.tick()
        self.assertEqual(calls,[])

    def test_consecutive_observe_errors_are_bounded_and_recorded(self):
        sid,oid,mid=self.observing(); self.engine.observe_retry_base=0
        calls=self.flaky(99,ValueError('timed out'))
        for _ in range(phase.OBSERVE_RETRY_LIMIT-1): self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'running')
        self.engine.tick(); op=self.engine.operation(oid,'owner-a')
        self.assertEqual((op['status'],op['failure']['code'],op['observe_errors']),('unknown','DELIVERY_UNKNOWN',phase.OBSERVE_RETRY_LIMIT))
        self.assertEqual((op['error']['type'],op['error']['message']),('ValueError','timed out'))
        self.assertEqual(len(calls),phase.OBSERVE_RETRY_LIMIT)
        got=self.call('bridge_operation_get',operation_id=oid)
        self.assertNotIn('timed out',json.dumps(got)); self.assertNotIn('error',got['data'])
        self.assertEqual((self.backend.posts,self.backend.pushes),(1,1))

    def test_reply_after_unknown_or_failed_close_is_attached(self):
        for state in ('unknown','failed'):
            with self.subTest(closed=state):
                sid,oid,mid=self.observing('late_reply_'+state+'_0001')
                if state=='unknown':
                    self.engine.observe_retry_limit=1; self.flaky(1); self.engine.tick()
                else:
                    self.backend.messages[mid]['state']='failed'; self.engine.tick()
                closed=self.engine.operation(oid,'owner-a')
                self.assertEqual(closed['status'],state); code=closed['failure']['code']
                if state=='unknown':
                    self.assertTrue(self.send(sid,key='independent_message_001')['ok'])
                    self.engine.tick()
                counts=(self.backend.posts,self.backend.pushes)
                self.engine.watch_at.clear(); self.engine.tick()
                self.assertEqual(self.engine.operation(oid,'owner-a')['status'],state)
                self.reply(mid,'late answer'); self.engine.tick()
                self.assertEqual(self.engine.operation(oid,'owner-a')['status'],state)  # Not due yet.
                self.restart(); self.backend.retire_reply=lambda m,parent:self.retired.append((m,parent))
                self.engine.tick()
                got=self.call('bridge_operation_get',operation_id=oid)['data']
                self.assertEqual((got['status'],got['result']['text'],got['failure']),('succeeded','late answer',None))
                self.assertEqual(self.engine.operation(oid,'owner-a')['settled_after'],{'status':state,'code':code})
                self.assertEqual(self.retired,[('r1',mid)])
                self.assertEqual((self.backend.posts,self.backend.pushes),counts)
                if state=='unknown':
                    self.assertTrue(self.send(sid,key='unblocked_message_1')['ok'])

    def test_late_settle_restarts_retention_from_the_success(self):
        sid,oid,mid=self.observing(); self.backend.messages[mid]['state']='failed'; self.engine.tick()
        op=self.engine.operation(oid,'owner-a'); self.assertEqual(op['status'],'failed')
        self.engine.update(op,finished_epoch=time.time()-phase.RETENTION+60)
        self.reply(mid,'late answer'); self.engine.watch_at.clear(); self.engine.tick()
        settled=self.engine.operation(oid,'owner-a')
        self.assertEqual(settled['status'],'succeeded')
        self.assertLess(time.time()-settled['finished_epoch'],30)
        with patch.object(phase.time,'time',return_value=time.time()+120): self.engine.tick()
        self.assertEqual(self.call('bridge_operation_get',operation_id=oid)['data']['result']['text'],'late answer')

    def test_reply_watch_never_changes_a_closed_operation_on_error(self):
        sid,oid,mid=self.observing(); self.backend.messages[mid]['state']='failed'; self.engine.tick()
        before=self.engine.operation(oid,'owner-a')
        self.flaky(1,OSError('down')); self.engine.watch_at.clear(); self.engine.tick()
        self.backend.replies[mid]=[{'id':'bad','reply_to':mid,'body':'no created field'}]
        self.engine.watch_at.clear(); self.engine.tick()
        after=self.engine.operation(oid,'owner-a')
        self.assertEqual((after['status'],after['failure']),(before['status'],before['failure']))
        self.assertFalse(self.engine.stop.is_set())

    def test_recovery_read_error_keeps_running_and_the_recovery_rule(self):
        sid=self.create()['agent_session_id']; oid=self.send(sid)['data']['operation_id']
        op=self.engine.operation(oid,'owner-a'); m=self.backend.post(op,{})
        self.engine.update(op,status='running',stage='posted',backend_id=m['id'],instance=self.backend.current)
        self.flaky(1); self.restart()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'running')
        self.engine.observe_at.clear(); self.engine.tick()
        self.assertEqual(self.engine.operation(oid,'owner-a')['status'],'unknown')
        self.assertEqual(self.backend.pushes,0)
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
        self.assertEqual(len(mcp_server.handle(req,'/missing',self.engine)['result']['tools']),6)
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

    def test_modern_protocol_flag_is_optional_and_strictly_boolean(self):
        path = self.root/'modern-config.json'
        for value in (False, True):
            config = dict(self.config, mcp_2026_enabled=value)
            path.write_text(json.dumps(config)); path.chmod(0o600)
            self.assertIs(phase.load_config(path)['mcp_2026_enabled'], value)
        for value in (None, 1, 'true', []):
            config = dict(self.config, mcp_2026_enabled=value)
            path.write_text(json.dumps(config)); path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'mcp_2026_enabled'):
                phase.load_config(path)
            with self.assertRaisesRegex(ValueError, 'mcp_2026_enabled'):
                phase.Engine(config, self.backend, start=False)

    def test_schema_contracts(self):
        if jsonschema:
            for tool in phase.TOOLS:
                jsonschema.Draft202012Validator.check_schema(tool['inputSchema'])
                jsonschema.Draft202012Validator.check_schema(tool['outputSchema'])

    def test_models_list_checks_scope_and_workspace_without_ledger_or_provider_writes(self):
        backend = phase.Backend(str(self.root / 'broker.sock'))
        self.engine.backend = backend
        workspace = self.config['workspaces']['test']
        before = dict(workspace)
        options = {'supports_model_selection': True, 'default_model': 'default-model',
                   'models': [{'id': 'default-model', 'label': 'Default model'}]}
        with patch.object(phase.models, 'session_model_options', return_value=options) as catalog, \
                patch.object(backend, 'rpc') as rpc, patch.object(phase.sessions, 'open_session') as open_session:
            self.assertIn('bridge_models_list', {t['name'] for t in self.engine.tools()})
            args = {'workspace_alias': 'test', 'agent_type': 'codex'}
            self.assertEqual(self.engine.call('bridge_models_list', args, 'other')['error']['code'],
                             'NOT_AUTHORIZED')
            scopes = self.engine.scopes
            self.engine.scopes = scopes - {'bridge:read'}
            self.assertEqual(self.call('bridge_models_list', **args)['error']['code'], 'NOT_AUTHORIZED')
            self.assertNotIn('bridge_models_list', {t['name'] for t in self.engine.tools()})
            self.engine.scopes = scopes
            self.assertEqual(self.call('bridge_models_list', workspace_alias='unknown')['error']['code'],
                             'WORKSPACE_NOT_ALLOWED')
            self.assertEqual(self.call('bridge_models_list', workspace_alias='test', agent_type='other')
                             ['error']['code'], 'INVALID_ARGUMENT')
            catalog.assert_not_called()
            workspace['host'] = 'ide'
            request = {'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call',
                       'params': {'name': 'bridge_models_list', 'arguments': args}}
            response = mcp_server.handle(request, backend.path, self.engine)
            self.assertEqual(response['result']['structuredContent'],
                             {'ok': True, 'data': {**args, **options}})
            catalog.assert_called_once_with(dict(workspace, agent_type='codex', host='desktop'),
                                            'codex', self.root / 'broker.state')
            catalog.reset_mock()
            no_options = {'supports_model_selection': False, 'default_model': None, 'models': []}
            catalog.return_value = no_options
            self.assertEqual(self.call('bridge_models_list', workspace_alias='test')['data'],
                             {'workspace_alias': 'test', 'agent_type': 'claude', **no_options})
            catalog.assert_called_once_with(dict(workspace, agent_type='claude'), 'claude',
                                            self.root / 'broker.state')
            catalog.reset_mock()
            del workspace['host']
            self.call('bridge_models_list', workspace_alias='test', agent_type='agy')
            catalog.assert_called_once_with(dict(workspace, agent_type='agy', host='desktop'),
                                            'agy', self.root / 'broker.state')
            catalog.side_effect = phase.models.ModelSelectionError('private details')
            failure = self.call('bridge_models_list', **args)
            self.assertEqual(failure['error']['code'], 'MODEL_UNAVAILABLE')
            self.assertNotIn('private details', json.dumps(failure))
            rpc.assert_not_called()
            open_session.assert_not_called()
        self.assertEqual(workspace, before)
        self.assertEqual(list(self.engine.rows()), [])
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM owned_sessions').fetchone()[0], 0)

    def test_create_model_idempotency_and_transient_workspace(self):
        args = {'workspace_alias': 'test', 'agent_type': 'agy', 'idempotency_key': 'create_model_0001'}
        explicit = dict(args, model='exact-model')
        with patch.object(self.backend, 'create', wraps=self.backend.create) as opening:
            for invalid in ('', ' 	 ', None, 'x' * 129, chr(0xd800)):
                self.assertEqual(self.call('bridge_session_create', **dict(args, model=invalid))
                                 ['error']['code'], 'INVALID_ARGUMENT')
            accepted = self.call('bridge_session_create', **explicit)['data']
            oid = accepted['operation_id']
            stored = self.engine.operation(oid, 'owner-a')
            self.assertEqual(stored['model'], 'exact-model')
            self.assertNotIn('model', stored['workspace'])
            self.restart()
            replay = self.call('bridge_session_create', **explicit)['data']
            self.assertEqual(replay['operation_id'], oid)
            self.assertTrue(replay['replayed'])
            for different in (args, dict(args, model='other-model')):
                self.assertEqual(self.call('bridge_session_create', **different)['error']['code'],
                                 'IDEMPOTENCY_CONFLICT')
            self.engine.tick()
            expected_workspace = self.config['workspaces']['test']
            opening.assert_called_once_with(dict(expected_workspace, agent_type='agy', model='exact-model'))
            session_id = self.call('bridge_operation_get', operation_id=oid)['data']['agent_session_id']
            self.assertEqual(self.engine.session(session_id, 'owner-a')['workspace'], expected_workspace)
            self.assertNotIn('model', expected_workspace)

        legacy = dict(args, idempotency_key='create_legacy_0001')
        oid = self.call('bridge_session_create', **legacy)['data']['operation_id']
        payload_hash = self.engine.db.execute('SELECT payload_hash FROM operations WHERE id=?', (oid,)).fetchone()[0]
        self.assertEqual(payload_hash, phase.digest({'contract_version': '0.1.0',
                                                     'workspace_alias': 'test', 'agent_type': 'agy'}))
        self.assertNotIn('model', self.engine.operation(oid, 'owner-a'))
        self.restart()
        self.assertTrue(self.call('bridge_session_create', **legacy)['data']['replayed'])
        self.assertEqual(self.call('bridge_session_create', **dict(legacy, model='exact-model'))
                         ['error']['code'], 'IDEMPOTENCY_CONFLICT')

    def test_create_model_claude_refused_before_admission(self):
        with patch.object(self.backend, 'instance') as instance:
            result = self.call('bridge_session_create', workspace_alias='test',
                               idempotency_key='create_claude_model', model='exact-model')
            self.assertEqual(result['error']['code'], 'UNSUPPORTED_MODEL_SELECTION')
            instance.assert_not_called()
        self.assertEqual(list(self.engine.rows()), [])

    def test_create_model_refusal_is_definite_and_never_opens_or_retries(self):
        backend = phase.Backend(str(self.root / 'broker.sock'))
        self.engine.backend = backend
        args = {'workspace_alias': 'test', 'agent_type': 'codex',
                'idempotency_key': 'create_refused_model', 'model': 'unavailable-model'}
        with patch.object(backend, 'instance', return_value='instance1'), \
                patch.object(phase.models, 'explicit_session_model',
                             side_effect=phase.models.ModelSelectionError('private details')) as resolve, \
                patch.object(phase.sessions, 'open_session') as opening:
            accepted = self.call('bridge_session_create', **args)['data']
            self.engine.tick()
            result = self.call('bridge_operation_get', operation_id=accepted['operation_id'])['data']
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['failure']['code'], 'MODEL_UNAVAILABLE')
            self.assertNotIn('private details', json.dumps(result))
            self.assertTrue(self.call('bridge_session_create', **args)['data']['replayed'])
            self.engine.tick()
            resolve.assert_called_once()
            opening.assert_not_called()

    def test_explicit_model_does_not_reclassify_ambiguous_open(self):
        backend = phase.Backend(str(self.root / 'broker.sock'))
        self.engine.backend = backend
        args = {'workspace_alias': 'test', 'agent_type': 'codex',
                'idempotency_key': 'create_ambiguous_model', 'model': 'exact-model'}
        with patch.object(backend, 'instance', return_value='instance1'), \
                patch.object(phase.models, 'explicit_session_model', return_value='exact-model'), \
                patch.object(phase.sessions, 'open_session', side_effect=RuntimeError('open reply lost')) as opening:
            accepted = self.call('bridge_session_create', **args)['data']
            self.engine.tick()
            result = self.call('bridge_operation_get', operation_id=accepted['operation_id'])['data']
            self.assertEqual((result['status'], result['failure']['code']), ('unknown', 'DELIVERY_UNKNOWN'))
            self.assertTrue(self.call('bridge_session_create', **args)['data']['replayed'])
            self.engine.tick()
            opening.assert_called_once()

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
    def test_facade_import_does_not_load_cli(self):
        scripts = str(Path(mcp_server.__file__).resolve().parent)
        code = """
import sys
sys.path.insert(0, sys.argv[1])
import mcp_server
import mcp_phase1
assert 'bridge' not in sys.modules
"""
        result = subprocess.run([sys.executable, '-I', '-c', code, scripts],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_and_mcp_dispatch_over_disposable_sockets(self):
        with RunningBroker() as running, FakeDesktop() as desktop:
            cwd = str(Path(running.temp.name).resolve())
            sid = str(uuid.uuid4())
            project = 'mcp-test'
            running.call('register', project=project, name='sender', cwd=cwd, transport='realtime')
            running.call('register', project=project, name='worker', cwd=cwd,
                         transport='codex', session=sid)
            config = {
                'principal': 'test-owner', 'tunnel_id': 'isolated-socket-test', 'writes_enabled': True,
                'scopes': list(set(phase.SCOPES.values())), 'state_dir': cwd + '/mcp',
                'workspaces': {'test': {'cwd': cwd, 'root': cwd, 'project': project, 'agent_type': 'codex'}},
                'pinned_sessions': {'worker': {'uuid': sid, 'cwd': cwd, 'project': project,
                                               'route': 'worker', 'authorized_at': phase.stamp(),
                                               'agent_type': 'codex'}},
            }
            engine = phase.Engine(config, phase.Backend(running.path), start=False)
            try:
                row = {'id': sid, 'cwd': cwd, 'status': 'idle'}
                with patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC': desktop.path}), \
                        patch.object(phase.sessions, 'resolve_codex_peer', return_value=row):
                    output = io.StringIO()
                    with patch.object(sys, 'stdin', io.StringIO('CLI fixture')), redirect_stdout(output):
                        code = bridge.main(['--socket', running.path, 'send', '--project', project,
                                            '--from', 'sender', '--to', 'worker', '--file', '-', '--key', 'direct'])
                    self.assertEqual(code, 0, output.getvalue())
                    direct = json.loads(output.getvalue())
                    self.assertEqual(running.call('show', id=direct['id'])['state'], 'submitted')

                    target = next(iter(phase.pinned_sessions(config)))
                    response = mcp_server.handle({
                        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'bridge_message_send', 'arguments': {
                            'agent_session_id': target, 'message': 'MCP fixture',
                            'idempotency_key': 'disposable_socket_01',
                        }},
                    }, running.path, engine)
                    accepted = response['result']['structuredContent']['data']
                    engine.tick()
                    op = engine.operation(accepted['operation_id'], 'test-owner')
                    self.assertEqual((op['status'], op['backend_message_state']), ('running', 'submitted'))
                    self.assertEqual(running.call('show', id=op['backend_id'])['state'], 'submitted')

                writes = [call['params'] for call in desktop.calls
                          if call.get('method') == 'thread-follower-steer-turn']
                self.assertEqual(len(writes), 2)
                for write, message_id, body in zip(
                        writes, (direct['id'], op['backend_id']), ('CLI fixture', 'MCP fixture')):
                    self.assertEqual((write['conversationId'], write['input']), (sid, []))
                    self.assertIn(message_id, write['toolOutput']['output'])
                    self.assertIn(body, write['toolOutput']['output'])
            finally:
                engine.close()

    def test_explicit_model_selection_forwards_canonical_value_to_existing_openers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            backend = phase.Backend(str(root / 'broker.sock'))
            sid = str(uuid.uuid4())
            for agent_type, host in (('codex', 'desktop'), ('agy', 'ide')):
                with self.subTest(agent_type=agent_type), \
                        patch.object(phase.models, 'explicit_session_model',
                                     return_value='canonical-model') as resolve, \
                        patch.object(phase.models, 'new_session_policy') as legacy_policy, \
                        patch.object(phase.sessions, 'open_session',
                                     return_value={'session': {'id': sid}}) as opening:
                    workspace = {'cwd': str(root), 'host': 'ide', 'agent_type': agent_type,
                                 'model': 'requested-model'}
                    self.assertEqual(backend.create(workspace), sid)
                    resolve.assert_called_once_with(dict(workspace, host=host), agent_type,
                                                    root / 'broker.state', 'requested-model')
                    opening.assert_called_once_with(agent_type, cwd=str(root), model='canonical-model',
                                                    host=host, timeout=180)
                    legacy_policy.assert_not_called()

    def test_codex_creation_uses_shared_default_and_owner_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            state = root / 'broker.state'
            state.mkdir()
            backend = phase.Backend(str(root / 'broker.sock'))
            sid = str(uuid.uuid4())
            workspace = {'cwd': str(root), 'agent_type': 'codex'}
            for env_model, policy_model, expected in (
                    (None, None, 'gpt-6.2-sol'),
                    (None, 'gpt-6.1-sol', 'gpt-6.1-sol'),
                    ('gpt-6.1-sol', 'owner-model', 'gpt-6.1-sol')):
                with self.subTest(env=env_model, policy=policy_model):
                    (state / 'policy.json').write_text(json.dumps({
                        'defaults': {'codex': policy_model} if policy_model else {}, 'deny': {}}))
                    env = dict(os.environ)
                    env.pop('AGENT_BRIDGE_CODEX_MODEL', None)
                    if env_model is not None:
                        env['AGENT_BRIDGE_CODEX_MODEL'] = env_model
                    with patch.dict(os.environ, env, clear=True), \
                            patch.object(phase.sessions, 'open_session',
                                         return_value={'session': {'id': sid}}) as opening:
                        self.assertEqual(backend.create(workspace), sid)
                    opening.assert_called_once_with('codex', cwd=str(root), model=expected,
                                                    host='desktop', timeout=180)

    def test_implicit_pin_retarget_during_dispatch_never_redirects_provider_write(self):
        for boundary in ('acceptance','availability','post','push'):
            with self.subTest(boundary=boundary), RunningBroker() as running, tempfile.TemporaryDirectory(dir='/tmp') as temp:
                root=str(Path(temp).resolve()); uuid_value=str(uuid.uuid4())
                config={'principal':'test-owner','tunnel_id':'isolated-retarget','writes_enabled':True,
                    'scopes':list(set(phase.SCOPES.values())),'state_dir':root+'/state',
                    'workspaces':{'test':{'cwd':root,'root':root,'project':'mcp-test','ide':sessions_ide()}},
                    'pinned_sessions':{'hq':{'uuid':uuid_value,'cwd':root,'project':'mcp-test',
                        'route':'hq','authorized_at':phase.stamp()}}}
                running.call('register',project='mcp-test',name='hq',cwd=root,transport='agy',session=uuid_value)
                backend=phase.Backend(running.path); engine=phase.Engine(config,backend,start=False)
                self.addCleanup(engine.close)
                row={'id':uuid_value,'cwd':root,'status':'running'}
                with patch.object(phase.sessions,'resolve_agy_peer',return_value=row), \
                     patch.object(phase.sessions,'resolve_codex_peer',return_value=row), \
                     patch.object(phase.delivery,'push_if_peer') as provider_write:
                    sid=next(iter(phase.pinned_sessions(config)))
                    args={'agent_session_id':sid, 'message':'must stay on original provider',
                          'idempotency_key':'dispatch_retarget_001'}
                    stage = 'require_send_availability' if boundary in ('acceptance', 'availability') else boundary
                    original=getattr(backend,stage)
                    def retarget(*args, **kwargs):
                        if boundary=='availability':
                            result=original(*args,**kwargs)
                        running.call('unregister',project='mcp-test',name='hq')
                        running.call('register',project='mcp-test',name='hq',cwd=root,transport='codex',session=uuid_value)
                        return result if boundary=='availability' else original(*args,**kwargs)
                    if boundary == 'acceptance':
                        setattr(backend,stage,retarget)
                        refused=engine.call('bridge_message_send',args)
                        self.assertEqual(refused['error']['message'], phase.ROUTE_CHANGED_MESSAGE)
                        self.assertEqual(engine.rows(), [])
                    else:
                        accepted=engine.call('bridge_message_send',args)['data']
                        setattr(backend,stage,retarget)
                        engine.tick()
                        operation=engine.operation(accepted['operation_id'],'test-owner')
                        expected = 'unknown' if boundary == 'push' else 'failed'
                        self.assertEqual(operation['status'], expected)
                    provider_write.assert_not_called()

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
                with patch.object(phase.sessions,'open_session',return_value={'session':row}) as opening, patch.object(phase.sessions,'resolve_claude_peer',return_value=row), patch.object(phase.models,'new_session_policy'):
                    accepted=engine.call('bridge_session_create',{'workspace_alias':'test','idempotency_key':'integrate_create01'})['data'];engine.tick()
                    op=engine.operation(accepted['operation_id'],'test-owner');self.assertEqual(op['status'],'succeeded')
                    opening.assert_called_once()
                    sent=engine.call('bridge_message_send',{'agent_session_id':op['agent_session_id'],'message':'hello','idempotency_key':'integrate_message1'})['data']
                    def mock_peer(path,m):
                        self.assertEqual(m['route']['session'],sid)
                        running.call('claim',id=m['id'])
                        return running.call('finish',id=m['id'],state='submitted',event='pushed')
                    with patch.object(phase.delivery,'push_if_peer',side_effect=mock_peer) as pushing:
                        engine.tick();pushing.assert_called_once()
                    op=engine.operation(sent['operation_id'],'test-owner');self.assertEqual(op['status'],'running')
                    running.call('reply',id=op['backend_id'],body='actual correlated reply',key='reply-key')
                    engine.tick();result=engine.get({'operation_id':op['operation_id']},'test-owner')
                    self.assertEqual(result['result']['text'],'actual correlated reply')
                    self.assertEqual(result['status'],'succeeded')
                    # Issue #134 against a real broker: the read fails once, the operation closes,
                    # the reply arrives pending, and a later read attaches and resolves it.
                    engine.observe_retry_limit=1
                    late=engine.call('bridge_message_send',{'agent_session_id':op['agent_session_id'],'message':'again','idempotency_key':'integrate_message2'})['data']
                    with patch.object(phase.delivery,'push_if_peer',side_effect=mock_peer), patch.object(backend,'observe',side_effect=TimeoutError('timed out')):
                        engine.tick()
                    op=engine.operation(late['operation_id'],'test-owner')
                    self.assertEqual((op['status'],op['backend_message_state'],op['error']['type']),('unknown','submitted','TimeoutError'))
                    answer=running.call('reply',id=op['backend_id'],body='late correlated reply',key='late-reply-key')
                    self.assertEqual(running.call('show',id=answer['id'])['state'],'pending')
                    engine.tick();result=engine.get({'operation_id':op['operation_id']},'test-owner')
                    self.assertEqual((result['status'],result['result']['text']),('succeeded','late correlated reply'))
                    self.assertEqual(running.call('show',id=answer['id'])['state'],'done')
            finally:
                engine.close()

    def test_multi_provider_discovery_pinned_and_send(self):
        with RunningBroker() as running, tempfile.TemporaryDirectory(prefix='mcp-multi-', dir='/tmp') as temp:
            root = str(Path(temp).resolve())
            wt = Path(root) / 'wt-agy'
            wt.mkdir()
            wt_str = str(wt.resolve())
            agy_sid = str(uuid.uuid4())
            codex_sid = str(uuid.uuid4())
            offline_codex_sid = str(uuid.uuid4())
            claude_sid = str(uuid.uuid4())
            pinned_agy_sid = str(uuid.uuid4())
            outside_sid = str(uuid.uuid4())

            running.call('register', project='mcp-test', name='agy-hq',
                         cwd=root, transport='agy', session=pinned_agy_sid)
            running.call('register', project='mcp-test', name='codex-offline',
                         cwd=root, transport='codex', session=offline_codex_sid)

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
                    'agy-pin': {
                        'uuid': pinned_agy_sid,
                        'cwd': root,
                        'project': 'mcp-test',
                        'route': 'agy-hq',
                        'authorized_at': phase.stamp(),
                    },
                },
            }
            backend = phase.Backend(running.path)
            engine = phase.Engine(config, backend, start=False)

            agy_rows = [
                {'provider': 'agy', 'id': agy_sid, 'name': 'Antigravity Worker Task', 'cwd': wt_str, 'status': 'running', 'updated_at': '2026-11-01T00:00:00Z'},
                {'provider': 'agy', 'id': pinned_agy_sid, 'name': 'Antigravity HQ Session', 'cwd': root, 'status': 'idle', 'updated_at': '2026-11-01T00:00:00Z'},
                {'provider': 'agy', 'id': outside_sid, 'name': 'Outside Session', 'cwd': '/tmp', 'status': 'idle', 'updated_at': '2026-11-01T00:00:00Z'},
            ]
            codex_rows = [
                {'provider': 'codex', 'id': codex_sid, 'name': 'Codex Thread', 'cwd': root, 'status': 'idle', 'updated_at': 1730419200},
            ]
            claude_rows = [
                {'provider': 'claude', 'id': claude_sid, 'name': 'Claude HQ', 'cwd': root, 'status': 'idle'},
            ]
            by_agy = {r['id']: r for r in agy_rows}

            try:
                with patch.object(phase.sessions, 'list_agy_peers', return_value=agy_rows), \
                        patch.object(phase.sessions, 'list_codex_peers', return_value=codex_rows), \
                        patch.object(phase.sessions, 'list_claude_peers', return_value=claude_rows), \
                        patch.object(phase.sessions, 'resolve_agy_peer', side_effect=lambda sid, **kw: by_agy[sid]), \
                        patch.object(phase.sessions, 'resolve_codex_peer', return_value=codex_rows[0]), \
                        patch.object(phase.sessions, 'resolve_claude_peer', return_value=claude_rows[0]):
                    status_call = {'jsonrpc': '2.0', 'id': 99, 'method': 'tools/call',
                                   'params': {'name': 'bridge_status', 'arguments': {}}}
                    status_data = mcp_server.handle(status_call, running.path, engine)['result']['structuredContent']['data']
                    self.assertEqual(status_data['supported_agent_types'], ['claude', 'agy', 'codex'])
                    self.assertTrue(status_data['runtime_revision'])
                    self.assertTrue(status_data['tool_schema_revision'])

                    listed = engine.call('bridge_sessions_list', {})['data']['sessions']
                    time.sleep(0.01)
                    listed_again = engine.call('bridge_sessions_list', {})['data']['sessions']
                    self.assertEqual(len(listed), 5)
                    for first_item, second_item in zip(listed, listed_again):
                        self.assertEqual(first_item['agent_session_id'], second_item['agent_session_id'])
                        self.assertEqual(first_item['created_at'], second_item['created_at'])
                        self.assertEqual(first_item['last_activity_at'], second_item['last_activity_at'])
                        self.assertTrue(second_item['observed_at'])

                    by_type = {}
                    for item in listed:
                        by_type.setdefault(item['agent_type'], []).append(item)
                    self.assertEqual(len(by_type['agy']), 2)
                    self.assertEqual(len(by_type['codex']), 2)
                    self.assertEqual(len(by_type['claude']), 1)

                    busy_agy = next(item for item in by_type['agy'] if item['state'] == 'busy')
                    pinned_agy = next(item for item in by_type['agy'] if item['workspace_alias'] == 'agy-pin')
                    live_codex = next(item for item in by_type['codex'] if item['state'] == 'ready')
                    offline_codex = next(item for item in by_type['codex'] if item['state'] == 'unavailable')
                    self.assertEqual(busy_agy['title'], 'Antigravity Worker Task')
                    self.assertIsNone(busy_agy['route_name'])
                    self.assertIsNone(busy_agy['created_at'])
                    self.assertEqual(busy_agy['last_activity_at'], '2026-11-01T00:00:00+00:00')
                    self.assertEqual(pinned_agy['title'], 'Antigravity HQ Session')
                    self.assertEqual(pinned_agy['route_name'], 'agy-hq')
                    self.assertEqual(live_codex['title'], 'Codex Thread')
                    self.assertIsNone(offline_codex['title'])
                    self.assertEqual(offline_codex['route_name'], 'codex-offline')
                    self.assertIsNone(offline_codex['created_at'])
                    self.assertIsNone(offline_codex['last_activity_at'])
                    self.assertEqual(by_type['claude'][0]['title'], 'Claude HQ')
                    self.assertIsNone(by_type['claude'][0]['created_at'])
                    self.assertIsNone(by_type['claude'][0]['last_activity_at'])
                    sent = engine.call('bridge_message_send', {
                        'agent_session_id': busy_agy['agent_session_id'],
                        'message': 'ping agy worker',
                        'idempotency_key': 'multi_send_agy_001',
                    })['data']

                    def mock_peer(path, m):
                        self.assertEqual(m['route']['session'], agy_sid)
                        self.assertEqual(m['route']['transport'], 'agy')
                        running.call('claim', id=m['id'])
                        return running.call('finish', id=m['id'], state='submitted', event='pushed')

                    with patch.object(phase.delivery, 'push_if_peer', side_effect=mock_peer) as pushing:
                        engine.tick()
                        pushing.assert_called_once()

                    op = engine.operation(sent['operation_id'], 'test-owner')
                    self.assertEqual(op['status'], 'running')
                    running.call('reply', id=op['backend_id'], body='agy reply ok', key='agy-reply-1')
                    engine.tick()
                    res = engine.get({'operation_id': op['operation_id']}, 'test-owner')
                    self.assertEqual(res['status'], 'succeeded')
                    self.assertEqual(res['result']['text'], 'agy reply ok')
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
                with patch.object(phase.sessions,'open_session',return_value={'session':row}), patch.object(phase.sessions,'resolve_claude_peer',return_value=row), patch.object(phase.models,'new_session_policy'):
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
                        with patch.object(phase.delivery,'push_if_peer',side_effect=push):e.tick()
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
