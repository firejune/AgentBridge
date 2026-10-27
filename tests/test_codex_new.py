import argparse
import functools
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import os
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import codex_live
import codex_new
import sessions
from support import RunningBroker
from test_codex_live import FakeDesktop


def full_permissions():
    return {'activePermissionProfile':{'id':codex_new.FULL_ACCESS_PROFILE,'extends':None},
            'approvalPolicy':'never','sandboxPolicy':{'type':'dangerFullAccess'}}


class ModelDesktop(FakeDesktop):
    def __init__(self, model='app-default', reject_update=False, keep_model=False, behavior='active',
                 permissions=None, refuse_permissions=False, stale_current=False, drop_update=False,
                 update_error=False):
        super().__init__(behavior)
        self.drop_update = drop_update
        self.update_error = update_error
        self.model, self.reject_update, self.keep_model = model, reject_update, keep_model
        self.permissions = permissions or {'activePermissionProfile':{'id':':workspace'},
            'approvalPolicy':'on-request','sandboxPolicy':{'type':'workspaceWrite'}}
        self.refuse_permissions, self.stale_current = refuse_permissions, stale_current

    def handle(self, sock, request):
        method = request['method']
        if request.get('type') == 'broadcast' and method == 'thread-stream-following-changed':
            self.calls.append(request)
            if request['params']['following']:
                state = {'id':request['params']['conversationId'],
                         'latestThreadSettings':self.permissions,
                         'currentPermissions':getattr(self,'current_permissions',{} if self.stale_current else self.permissions)}
                self.write(sock, {'type':'broadcast','method':'thread-stream-state-changed','version':11,
                    'sourceClientId':self.owner,'targetClientIds':[request['sourceClientId']],
                    'params':{'hostId':'local','conversationId':state['id'],
                              'change':{'type':'snapshot','revision':1,'conversationState':state}}})
            return True
        if method not in ('thread-follower-read-model-settings','thread-follower-update-thread-settings'):
            return super().handle(sock, request)
        self.calls.append(request)
        if self.drop_update and method == 'thread-follower-update-thread-settings':
            return False  # Possibly applied, never answered.
        response = {'type':'response','requestId':request['requestId'],'resultType':'success',
                    'method':method,'handledByClientId':self.owner}
        if method == 'thread-follower-read-model-settings':
            response['result'] = {'settings':{'model':self.model,'resumeState':'resumed'}}
        else:
            if self.update_error:
                response.update(resultType='error', error='secret /private/transport')
                self.write(sock, response)
                return True
            values = request['params']['threadSettings']
            if not self.keep_model and 'model' in values:
                self.model = values['model']
            if not self.refuse_permissions and 'permissions' in values:
                self.permissions = full_permissions()
            response['result'] = {'applied':not self.reject_update}
        self.write(sock, response)
        return True


class NewCodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = str(Path(self.temp.name).resolve())
        self.sid = str(uuid.uuid4())
        self.urls, self.calls, self.instances = [], [], []
        self.thread = {'id':self.sid,'provider':'codex','cwd':self.root,'ephemeral':False,
                       'source':'vscode','status':{'type':'idle'},'name':None,'model':'requested-model'}
        self.fail_method = None
        self.reader_wrong_cwd = False
        self.substitute_model = False
        self.profiles = [{'id':codex_new.FULL_ACCESS_PROFILE,'allowed':True}]
        self.creator_full_access = True
        self.catalog = [{'model':'requested-model'},{'model':codex_new.DEFAULT_MODEL}]
        self.saved_pages = {None: {'data': [], 'nextCursor': None}}
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(codex_new.MODEL_ENV, None)

    def tearDown(self):
        self.temp.cleanup()

    def factory(self, **kwargs):
        test = self
        class Client:
            def __init__(self):
                self.closed = False
                self.timeout = kwargs['timeout']
                self.creator = kwargs.get('experimental',False)
                test.instances.append(self)
            def __enter__(self):return self
            def __exit__(self,*_):self.closed = True
            def call(self, method, params):
                test.calls.append((method,params,self))
                if test.fail_method == method:
                    raise sessions.DiscoveryError('ambiguous disconnect')
                if method == 'permissionProfile/list':
                    return {'data':test.profiles,'nextCursor':None}
                if method == 'model/list':
                    return {'data':test.catalog,'nextCursor':None}
                if method == 'thread/list':
                    return test.saved_pages[params.get('cursor')]
                if method == 'thread/start':
                    return {'thread':dict(test.thread),'model':'substituted' if test.substitute_model else params.get('model','creator-default'),
                            'cwd':test.root, 'approvalPolicy':'never' if test.creator_full_access else 'on-request',
                            'activePermissionProfile':{'id':codex_new.FULL_ACCESS_PROFILE},
                            'sandbox':{'type':'dangerFullAccess' if test.creator_full_access else 'workspaceWrite'}}
                if method == 'thread/name/set':
                    test.thread['name'] = params['name'];return {}
                if method == 'thread/read':
                    row = dict(test.thread)
                    if test.reader_wrong_cwd:row['cwd'] = '/other'
                    return {'thread':row}
                raise AssertionError(method)
        return Client()

    def open(self, desktop, **kwargs):
        kwargs.setdefault('client_factory',self.factory)
        kwargs.setdefault('opener',self.urls.append)
        kwargs.setdefault('timeout',.1)
        return codex_new.open_codex(self.root,owner_factory=lambda **kw:codex_live.Client(desktop.path,**kw),
                                   poll=.005,**kwargs)

    def test_autonomous_create_close_adopt_verify_and_set_exact_model(self):
        def adopt(url):
            self.assertTrue(all(c.closed for c in self.instances))
            self.urls.append(url)
        with ModelDesktop() as desktop:
            result = self.open(desktop,title='Exact requested title',model='requested-model',opener=adopt)
            self.assertTrue(result['created'])
            self.assertTrue(result['adopted'])
            self.assertEqual(result['session']['id'],self.sid)
            self.assertEqual(result['session']['name'],'Exact requested title')
            self.assertEqual(result['model'],'requested-model')
            self.assertEqual(self.urls,['codex://threads/'+self.sid])
            create = next(params for method,params,_ in self.calls if method=='thread/start')
            self.assertEqual(create,{'cwd':self.root,'model':'requested-model','ephemeral':False,'allowProviderModelFallback':False,
                                     'permissions':codex_new.FULL_ACCESS_PROFILE,'approvalPolicy':'never'})
            self.assertTrue(all(c.closed for c in self.instances))
            self.assertTrue(all(method not in ('turn/start','turn/steer') for method,_,_ in self.calls))
            update = next(x for x in desktop.calls if x['method']=='thread-follower-update-thread-settings')
            self.assertEqual(update['params'],{'conversationId':self.sid,'threadSettings':{
                'model':'requested-model','permissions':codex_new.FULL_ACCESS_PROFILE,'approvalPolicy':'never'}})
            self.assertEqual(result['permission_settings'],full_permissions())
            self.assertEqual(update['version'],2)

    def test_omitted_model_requests_default_not_app_default_and_full_access(self):
        with ModelDesktop(model='app-default',permissions=full_permissions()) as desktop:
            result = self.open(desktop)
        self.assertEqual(codex_new.DEFAULT_MODEL,'gpt-6.1-sol')
        self.assertTrue(any(method=='model/list' for method,_,_ in self.calls))
        create = next(params for method,params,_ in self.calls if method=='thread/start')
        self.assertEqual(create['model'],codex_new.DEFAULT_MODEL)
        self.assertNotIn('sandbox',create)
        self.assertEqual(create['permissions'],codex_new.FULL_ACCESS_PROFILE)
        self.assertEqual(create['approvalPolicy'],'never')
        self.assertEqual(result['model'],codex_new.DEFAULT_MODEL)
        self.assertEqual(result['model_source'],'default')
        self.assertEqual(result['creator_settings']['approvalPolicy'],'never')
        update = [x for x in desktop.calls if x['method']=='thread-follower-update-thread-settings']
        self.assertEqual([x['params']['threadSettings'] for x in update],[{'model':codex_new.DEFAULT_MODEL}])
        self.assertNotIn('prompt',result)

    def test_owner_policy_default_reaches_creation_and_readback(self):
        policy = {'defaults': {'codex': 'requested-model'}, 'deny': {}}
        with ModelDesktop(permissions=full_permissions()) as desktop:
            result = self.open(desktop, policy=policy)
        create = next(params for method,params,_ in self.calls if method=='thread/start')
        self.assertEqual(create['model'], 'requested-model')
        self.assertEqual((result['model'], result['model_source']), ('requested-model', 'policy'))

    def test_supplied_policy_refuses_denied_flag_env_and_default_before_any_client(self):
        policy = {'path': 'policy.json', 'exists': True,
                  'defaults': {'codex': 'gpt-6-astra'}, 'deny': {'codex': ['gpt-6-astra']}}
        for model, env in (('gpt-6-astra', {}), (None, {codex_new.MODEL_ENV: 'gpt-6-astra'}), (None, {})):
            with self.subTest(model=model, env=bool(env)), patch.dict(os.environ, env), \
                    patch.object(sessions, 'CodexClient') as creator, \
                    patch.object(codex_live, 'Client') as owner, \
                    self.assertRaisesRegex(codex_new.CreationError, 'denylist') as caught:
                codex_new.open_codex(self.root, model=model, policy=policy, opener=self.urls.append)
            self.assertFalse(caught.exception.uncertain)
            self.assertFalse(caught.exception.created)
            creator.assert_not_called()
            owner.assert_not_called()
        self.assertEqual(self.urls, [])

    def test_env_overrides_default_and_flag_overrides_env(self):
        os.environ[codex_new.MODEL_ENV] = 'requested-model'
        with ModelDesktop(permissions=full_permissions()) as desktop:
            result = self.open(desktop)
        self.assertEqual(result['model'],'requested-model')
        self.assertEqual(result['model_source'],codex_new.MODEL_ENV)
        self.assertEqual(codex_new.resolve_model('explicit',{codex_new.MODEL_ENV:'requested-model'}),('explicit','--model'))
        with self.assertRaisesRegex(codex_new.CreationError,codex_new.MODEL_ENV):
            codex_new.resolve_model(None,{codex_new.MODEL_ENV:' '})

    def test_unadvertised_default_fails_by_name_and_never_falls_back(self):
        self.catalog = [{'model':'app-default'}]
        with ModelDesktop(model='app-default') as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop)
        self.assertIn(repr(codex_new.DEFAULT_MODEL),str(caught.exception))
        self.assertIn('pass --model',str(caught.exception))
        self.assertFalse(caught.exception.created)
        self.assertFalse(any(method=='thread/start' for method,_,_ in self.calls))
        self.assertEqual(self.urls,[])

    def test_unsupported_model_refused_before_creation(self):
        with ModelDesktop() as desktop, self.assertRaisesRegex(codex_new.CreationError,'not advertised') as caught:
            self.open(desktop,model='missing')
        self.assertFalse(caught.exception.created)
        self.assertFalse(caught.exception.uncertain)
        self.assertFalse(any(method=='thread/start' for method,_,_ in self.calls))
        self.assertEqual(self.urls,[])
        self.assertTrue(all(c.closed for c in self.instances))

    def test_missing_or_disallowed_full_access_profile_refused_before_creation(self):
        for profiles in ([],[{'id':codex_new.FULL_ACCESS_PROFILE,'allowed':False}]):
            self.profiles=profiles
            with self.subTest(profiles=profiles), ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
                self.open(desktop)
            self.assertFalse(caught.exception.created)
            self.assertFalse(caught.exception.uncertain)
        self.assertFalse(any(method=='thread/start' for method,_,_ in self.calls))

    def test_creator_must_confirm_full_access_and_never(self):
        self.creator_full_access=False
        with ModelDesktop() as desktop, self.assertRaisesRegex(codex_new.CreationError,'creator did not confirm') as caught:
            self.open(desktop)
        self.assertTrue(caught.exception.created)
        self.assertEqual(self.urls,[])

    def test_latest_settings_override_stale_current_permissions_for_next_turn(self):
        with ModelDesktop(model=codex_new.DEFAULT_MODEL,permissions=full_permissions(),stale_current=True) as desktop:
            result=self.open(desktop)
        self.assertEqual(result['permission_settings'],full_permissions())
        self.assertFalse(any(x['method']=='thread-follower-update-thread-settings' for x in desktop.calls))

    def test_latest_permissions_mismatch_is_not_masked_by_update_ack(self):
        with ModelDesktop(refuse_permissions=True) as desktop, self.assertRaisesRegex(codex_new.CreationError,'did not confirm Full Access') as caught:
            desktop.current_permissions=full_permissions()
            self.open(desktop)
        self.assertTrue(caught.exception.adopted)
        self.assertEqual(caught.exception.stage,'permissions')
        self.assertEqual(sum(x['method']=='thread-follower-update-thread-settings' for x in desktop.calls),1)

    def cli_open(self, desktop, *extra):
        real_open = codex_new.open_codex
        def create(**kwargs):
            return real_open(**kwargs, client_factory=self.factory, opener=self.urls.append,
                             owner_factory=lambda **kw: codex_live.Client(desktop.path, **kw), poll=.005)
        out = io.StringIO()
        with patch.object(codex_new, 'open_codex', side_effect=create), patch('sys.stdout', out):
            code = bridge.main(['open', '--provider', 'codex', '--cwd', self.root,
                                '--model', 'requested-model', '--state-dir', self.root, *extra])
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, out.getvalue())
        value = json.loads(lines[0])
        self.assertIsInstance(value, dict)
        return code, value

    def test_issue44_shape_success_refusal_and_early_failure(self):
        with ModelDesktop() as desktop:
            code, success = self.cli_open(desktop)
        self.assertEqual(code, 0)
        with ModelDesktop(refuse_permissions=True) as desktop:
            code, refusal = self.cli_open(desktop)
        self.assertEqual(code, 1)
        with ModelDesktop() as desktop:
            code, early = self.cli_open(desktop, '--timeout', '0')
        self.assertEqual(code, 1)
        self.assertEqual(set(success), set(refusal))
        self.assertEqual(set(success), set(early))
        self.assertIsInstance(early['session'], dict)
        self.assertIsNone(early['session']['id'])
        self.assertEqual(set(success['session']), set(early['session']))
        self.assertEqual(refusal['session']['id'], self.sid)
        self.assertEqual(refusal['session']['cwd'], self.root)
        self.assertEqual(success['stage'], 'complete')
        self.assertFalse(success['uncertain'])

    def test_issue44_shape_policy_and_registration_failure(self):
        with ModelDesktop() as desktop:
            _, success = self.cli_open(desktop)
            with patch.object(bridge, 'new_session_policy', side_effect=ValueError('secret /private/config')):
                code, failure = self.cli_open(desktop)
            self.assertEqual(code, 1)
            self.assertEqual(set(success), set(failure))
            self.assertNotIn('secret', json.dumps(failure))
            self.assertNotIn('/private/config', json.dumps(failure))
            with patch.object(bridge.broker, 'request', side_effect=OSError('secret /private/socket')):
                code, failure = self.cli_open(desktop, '--register', 'worker', '--project', 'demo')
            self.assertEqual(code, 1)
            self.assertEqual(set(success), set(failure))
            self.assertEqual(failure['session']['id'], self.sid)
            self.assertFalse(failure['registered'])
            self.assertNotIn('secret', json.dumps(failure))

    def test_issue44_diagnostics_effective_permissions_and_update(self):
        with ModelDesktop(refuse_permissions=True) as desktop, self.assertRaises(codex_new.CreationError) as caught:
            expected = json.loads(json.dumps(desktop.permissions))
            desktop.permissions['credential'] = 'secret'
            desktop.permissions['sandboxPolicy']['writableRoots'] = ['/private/secret']
            self.open(desktop)
        result = caught.exception.result
        self.assertEqual(result['permission_settings'], expected)
        self.assertEqual(result['settings_update'], {'applied': True, 'resultType': 'success'})
        self.assertEqual(result['permission_settings_source'], 'Desktop owner latestThreadSettings')
        self.assertNotIn('secret', json.dumps(result))

    def test_issue44_diagnostics_rejected_ack_still_reads_effective_settings(self):
        with ModelDesktop(model='requested-model', refuse_permissions=True, reject_update=True) as desktop:
            code, result = self.cli_open(desktop)
        self.assertEqual(code, 1)
        self.assertEqual(result['settings_update'], {'applied': False, 'resultType': 'success'})
        self.assertEqual(result['permission_settings']['approvalPolicy'], 'on-request')
        self.assertEqual(result['stage'], 'permissions')
        self.assertEqual(sum(x['method']=='thread-follower-update-thread-settings' for x in desktop.calls), 1)

    def test_issue44_registration_on_permission_refusal_preserves_failure(self):
        with RunningBroker() as server, ModelDesktop(refuse_permissions=True) as desktop:
            # Patch only the broker destination; creation still uses real fixture IPC.
            original = bridge.broker.request
            with patch.object(bridge.broker, 'request', side_effect=lambda _, fields, **kw: original(server.path, fields, **kw)):
                code, result = self.cli_open(desktop, '--register', 'worker', '--project', 'demo')
            self.assertEqual(code, 1)
            self.assertTrue(result['registered'])
            self.assertTrue(result['created'])
            self.assertTrue(result['adopted'])
            self.assertEqual(result['stage'], 'permissions')
            self.assertIsNotNone(result['error'])
            self.assertEqual(server.call('routes', project='demo')[0]['session'], self.sid)

    def test_issue44_registration_refuses_unadopted_and_model_failure(self):
        for stage, adopted in (('adoption', False), ('model', True)):
            failure = codex_new.CreationError('failed', True, self.sid, adopted, stage)
            with RunningBroker() as server, patch.object(codex_new, 'open_codex', side_effect=failure):
                out = io.StringIO()
                with patch('sys.stdout', out):
                    code = bridge.main(['--socket', server.path, 'open', '--provider', 'codex', '--cwd', self.root,
                                        '--register', 'worker', '--project', 'demo', '--state-dir', self.root])
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(out.getvalue())['registered'])
                self.assertEqual(server.call('routes', project='demo'), [])

    def test_issue44_duplicate_scans_all_pages_and_never_touches_owner(self):
        self.saved_pages = {
            None: {'data': [dict(self.thread, id=str(uuid.uuid4()), name='other') for _ in range(100)], 'nextCursor': 'second'},
            'second': {'data': [dict(self.thread, name='Worker')], 'nextCursor': None}}
        with ModelDesktop(refuse_permissions=True) as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop, title='Worker')
        self.assertEqual(caught.exception.stage, 'duplicate')
        self.assertFalse(caught.exception.created)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.result['session']['id'], self.sid)
        self.assertEqual(sum(m=='thread/list' for m,_,_ in self.calls), 2)
        self.assertFalse(any(m in ('thread/start', 'thread/name/set') for m,_,_ in self.calls))
        self.assertEqual(desktop.calls, [])
        self.assertEqual(self.urls, [])

    def test_issue44_duplicate_multiple_matches_no_selection_and_new_escape(self):
        other = str(uuid.uuid4())
        self.saved_pages = {None: {'data': [dict(self.thread, name='Worker'), dict(self.thread, id=other, name='Worker')], 'nextCursor': None}}
        with ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop, title='Worker')
        self.assertIsNone(caught.exception.result['session']['id'])
        self.assertEqual(len(caught.exception.result['existing_sessions']), 2)
        self.assertIn('--new', str(caught.exception))
        self.assertEqual(desktop.calls, [])
        self.calls.clear()
        with ModelDesktop() as desktop:
            result = self.open(desktop, title='Worker', force_new=True)
        self.assertTrue(result['created'])
        self.assertFalse(any(m=='thread/list' for m,_,_ in self.calls))

    def test_issue44_duplicate_repeated_cursor_refuses_without_creating(self):
        self.saved_pages = {None: {'data': [], 'nextCursor': 'again'}, 'again': {'data': [], 'nextCursor': 'again'}}
        with ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop, title='Worker')
        self.assertFalse(caught.exception.created)
        self.assertEqual(caught.exception.stage, 'duplicate')
        self.assertEqual(desktop.calls, [])
        self.assertFalse(any(m=='thread/start' for m,_,_ in self.calls))

    def test_issue44_duplicate_exact_title_and_resolved_cwd_controls(self):
        self.saved_pages = {None: {'data': [dict(self.thread, name='worker'),
            dict(self.thread, name='Worker', cwd=self.root + '/elsewhere')], 'nextCursor': None}}
        with ModelDesktop() as desktop:
            result = self.open(desktop, title='Worker')
        self.assertTrue(result['created'])
        self.saved_pages = {None: {'data': [dict(self.thread, name='Worker', cwd=self.root + '/.')], 'nextCursor': None}}
        with ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop, title='Worker')
        self.assertEqual(caught.exception.stage, 'duplicate')
        self.assertEqual(desktop.calls, [])

    def test_issue44_duplicate_malformed_page_never_creates(self):
        for page in ({'data': [], 'nextCursor': 12}, {'data': None},
                     {'data': [dict(self.thread, name='Worker', id='invalid')]},
                     {'data': [dict(self.thread, name='Worker', cwd=None)]}):
            with self.subTest(page=page):
                self.saved_pages = {None: page}
                self.calls.clear()
                with ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
                    self.open(desktop, title='Worker')
                self.assertEqual(caught.exception.stage, 'duplicate')
                self.assertFalse(caught.exception.created)
                self.assertFalse(any(m=='thread/start' for m,_,_ in self.calls))
                self.assertEqual(desktop.calls, [])

    def test_issue44_cli_new_skips_duplicate_scan(self):
        self.saved_pages = {None: {'data': [dict(self.thread, name='Worker')], 'nextCursor': None}}
        with ModelDesktop() as desktop:
            code, result = self.cli_open(desktop, '--title', 'Worker', '--new')
        self.assertEqual(code, 0)
        self.assertTrue(result['created'])
        self.assertFalse(any(m=='thread/list' for m,_,_ in self.calls))

    def test_issue44_ambiguous_create_has_null_id_and_no_retry(self):
        self.fail_method = 'thread/start'
        with ModelDesktop() as desktop:
            code, result = self.cli_open(desktop)
        self.assertEqual(code, 1)
        self.assertTrue(result['uncertain'])
        self.assertFalse(result['created'])
        self.assertIsNone(result['session']['id'])
        self.assertEqual(sum(m=='thread/start' for m,_,_ in self.calls), 1)
        self.assertEqual(desktop.calls, [])

    def test_issue44_registration_both_failures_preserve_refusal_and_one_attempt(self):
        with ModelDesktop(refuse_permissions=True) as desktop, \
                patch.object(bridge.broker, 'request', side_effect=OSError('secret socket')) as register:
            code, result = self.cli_open(desktop, '--register', 'worker', '--project', 'demo')
        self.assertEqual(code, 1)
        self.assertEqual(result['stage'], 'permissions')
        self.assertIn('Full Access', result['error'])
        self.assertIsNotNone(result['registration_error'])
        self.assertFalse(result['registered'])
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(register.call_count, 1)

    def test_issue44_registration_success_and_refusal_share_keys(self):
        with ModelDesktop() as desktop, patch.object(bridge.broker, 'request', return_value={}) as register:
            code, success = self.cli_open(desktop, '--register', 'worker', '--project', 'demo')
        self.assertEqual(code, 0)
        self.assertTrue(success['registered'])
        self.assertEqual(register.call_count, 1)
        with ModelDesktop(refuse_permissions=True) as desktop, patch.object(bridge.broker, 'request', return_value={}):
            code, refusal = self.cli_open(desktop, '--register', 'worker', '--project', 'demo')
        self.assertEqual(code, 1)
        self.assertEqual(set(success), set(refusal))
        self.assertTrue(refusal['registered'])

    def test_issue44_registration_invalid_pair_stops_before_creation(self):
        for extra in (('--register', 'worker'), ('--project', 'demo')):
            with ModelDesktop() as desktop:
                code, result = self.cli_open(desktop, *extra)
            self.assertEqual(code, 1)
            self.assertIsNone(result['session']['id'])
            self.assertFalse(result['created'])
        self.assertEqual(self.calls, [])

    def test_issue44_registration_register_new_keeps_permission_refusal(self):
        real_open = codex_new.open_codex
        with ModelDesktop(refuse_permissions=True) as desktop:
            def create(**kwargs):
                self.assertTrue(kwargs['force_new'])
                return real_open(**kwargs, client_factory=self.factory, opener=self.urls.append,
                                 owner_factory=lambda **kw: codex_live.Client(desktop.path, **kw))
            with patch.object(codex_new, 'open_codex', side_effect=create), \
                    patch.object(bridge.broker, 'request', return_value={}) as register:
                out = io.StringIO()
                with patch('sys.stdout', out):
                    code = bridge.main(['register', 'worker', '--project', 'demo', '--transport', 'codex',
                                        '--new', '--cwd', self.root, '--state-dir', self.root])
        value = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertTrue(value['registered'])
        self.assertEqual(value['stage'], 'permissions')
        self.assertEqual(register.call_count, 1)

    def test_issue44_diagnostics_unknown_values_are_redacted(self):
        clean = codex_new.permission_diagnostics({'approvalPolicy': {'secret': '/private/config'},
            'activePermissionProfile': {'id': 'secret', 'extends': '/private/config'},
            'sandboxPolicy': {'type': 'secret', 'credential': 'secret'}})
        self.assertNotIn('secret', json.dumps(clean))
        self.assertNotIn('/private', json.dumps(clean))
        self.assertFalse(codex_new.has_full_access(clean))

    def test_issue44_diagnostics_ambiguous_settings_update_never_replays(self):
        with ModelDesktop(drop_update=True) as desktop:
            code, result = self.cli_open(desktop)
        self.assertEqual(code, 1)
        self.assertTrue(result['created'])
        self.assertTrue(result['adopted'])
        self.assertTrue(result['uncertain'])
        self.assertEqual(result['settings_update'], {'applied': None, 'resultType': None})
        self.assertEqual(sum(x['method']=='thread-follower-update-thread-settings' for x in desktop.calls), 1)

    def test_issue44_diagnostics_error_response_reports_type_without_payload(self):
        with ModelDesktop(model='requested-model', refuse_permissions=True, update_error=True) as desktop:
            code, result = self.cli_open(desktop)
        self.assertEqual(code, 1)
        self.assertEqual(result['stage'], 'permissions')
        self.assertEqual(result['settings_update'], {'applied': None, 'resultType': 'error'})
        self.assertEqual(result['permission_settings']['approvalPolicy'], 'on-request')
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('/private/transport', json.dumps(result))
        self.assertEqual(sum(x['method']=='thread-follower-update-thread-settings' for x in desktop.calls), 1)

    def test_issue44_retry_same_title_and_cwd_refuses_saved_thread(self):
        with ModelDesktop(refuse_permissions=True) as desktop:
            code, first = self.cli_open(desktop, '--title', 'Worker')
        self.assertEqual(code, 1)
        self.saved_pages = {None: {'data': [dict(self.thread)], 'nextCursor': None}}
        with ModelDesktop() as desktop:
            code, retry = self.cli_open(desktop, '--title', 'Worker')
        self.assertEqual(code, 1)
        self.assertEqual(retry['stage'], 'duplicate')
        self.assertEqual(retry['session']['id'], first['session']['id'])
        self.assertFalse(retry['created'])
        self.assertEqual(sum(m=='thread/start' for m,_,_ in self.calls), 1)
        self.assertEqual(desktop.calls, [])

    def test_creator_substitution_is_not_accepted(self):
        self.substitute_model = True
        with ModelDesktop() as desktop, self.assertRaisesRegex(codex_new.CreationError,'substituted') as caught:
            self.open(desktop,model='requested-model')
        self.assertTrue(caught.exception.created)
        self.assertEqual(self.urls,[])

    def test_unknown_create_failure_never_retries(self):
        self.fail_method='thread/start'
        with ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop)
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(caught.exception.created)
        self.assertEqual(sum(method=='thread/start' for method,_,_ in self.calls),1)
        self.assertTrue(all(c.closed for c in self.instances))

    def test_title_failure_preserves_created_id(self):
        self.fail_method='thread/name/set'
        with ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop,title='Title')
        self.assertEqual(caught.exception.session,self.sid)
        self.assertTrue(caught.exception.created)
        self.assertFalse(caught.exception.adopted)
        self.assertEqual(caught.exception.stage,'title')
        self.assertEqual(self.urls,[])

    def test_persistence_cwd_mismatch_stops_before_adoption(self):
        self.reader_wrong_cwd=True
        with ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop)
        self.assertTrue(caught.exception.created)
        self.assertEqual(caught.exception.stage,'persistence')
        self.assertEqual(self.urls,[])

    def test_no_owner_never_registers_or_creates_replacement(self):
        with ModelDesktop(behavior='no-owner') as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop,timeout=.02)
        self.assertTrue(caught.exception.created)
        self.assertFalse(caught.exception.adopted)
        self.assertEqual(self.urls,['codex://threads/'+self.sid])
        self.assertEqual(sum(method=='thread/start' for method,_,_ in self.calls),1)

    def test_launcher_timeout_keeps_recovery_id(self):
        def fail(url):raise subprocess.TimeoutExpired(['open',url],10)
        with ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop,opener=fail)
        self.assertEqual(caught.exception.session,self.sid)
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(caught.exception.adopted)

    def test_unexpected_post_create_error_preserves_recovery_identity(self):
        def fail(url):raise RuntimeError('unexpected launcher adapter failure')
        with ModelDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop,opener=fail)
        self.assertEqual(caught.exception.session,self.sid)
        self.assertTrue(caught.exception.created)
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(caught.exception.adopted)
        self.assertEqual(caught.exception.stage,'adoption')
        self.assertEqual(sum(method=='thread/start' for method,_,_ in self.calls),1)
        self.assertTrue(all(c.closed for c in self.instances))

    def test_model_update_refusal_and_readback_substitution_fail(self):
        for options in ({'reject_update':True},{'keep_model':True}):
            with self.subTest(options=options), ModelDesktop(**options) as desktop, self.assertRaises(codex_new.CreationError) as caught:
                self.open(desktop,model='requested-model')
            self.assertTrue(caught.exception.adopted)
            self.assertEqual(sum(x['method']=='thread-follower-update-thread-settings' for x in desktop.calls),1)

    def test_missing_model_readback_fails_after_adoption(self):
        with ModelDesktop(model=None) as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop)
        self.assertTrue(caught.exception.adopted)

    def test_input_refusals_precede_creation(self):
        for kwargs in ({'model':''},{'title':' '},{'timeout':0},{'timeout':float('nan')},{'timeout':float('inf')}):
            with self.subTest(kwargs=kwargs), self.assertRaises(codex_new.CreationError):
                codex_new.open_codex(self.root,client_factory=self.factory,opener=self.urls.append,**kwargs)
        self.assertEqual(self.instances,[])
        with self.assertRaises(codex_new.CreationError):
            codex_new.open_codex(self.root+'/absent',client_factory=self.factory,opener=self.urls.append)

    def test_open_register_cli_and_registration_failure_preserve_identity(self):
        result = codex_new.open_result()
        result.update(created=True, adopted=True, session=sessions.codex_row(self.thread, 'saved'), stage='complete')
        with RunningBroker() as server, patch.object(codex_new,'open_codex',side_effect=lambda **_: json.loads(json.dumps(result))) as create:
            out=io.StringIO()
            with patch('sys.stdout',out):
                code=bridge.main(['--socket',server.path,'register','worker','--project','demo','--transport','codex',
                                  '--new','--cwd',self.root,'--title','Real title','--model','requested-model'])
            self.assertEqual(code,0,out.getvalue())
            self.assertTrue(json.loads(out.getvalue())['registered'])
            self.assertEqual(create.call_args.kwargs['model'],'requested-model')
            self.assertEqual(server.call('routes',project='demo')[0]['session'],self.sid)
        with patch.object(codex_new,'open_codex',side_effect=lambda **_: json.loads(json.dumps(result))) as create:
            out=io.StringIO()
            with patch('sys.stdout',out):
                code=bridge.main(['--socket',self.root+'/absent.sock','register','worker','--project','demo',
                                  '--transport','codex','--new','--cwd',self.root])
            value=json.loads(out.getvalue())
            self.assertEqual(code,1)
            self.assertTrue(value['created'])
            self.assertTrue(value['adopted'])
            self.assertFalse(value['registered'])
            self.assertEqual(value['session']['id'],self.sid)
            self.assertEqual(create.call_count,1)

    def test_partial_failure_cli_reports_stage_and_never_registers(self):
        failure=codex_new.CreationError('failed',True,self.sid,True,'model')
        with RunningBroker() as server, patch.object(codex_new,'open_codex',side_effect=failure) as create:
            out=io.StringIO()
            with patch('sys.stdout',out):
                code=bridge.main(['--socket',server.path,'register','worker','--project','demo','--transport','codex','--new','--cwd',self.root])
            value=json.loads(out.getvalue())
            self.assertEqual(code,1)
            self.assertEqual(value['stage'],'model')
            self.assertTrue(value['created'])
            self.assertTrue(value['adopted'])
            self.assertTrue(value['uncertain'])
            self.assertFalse(value['registered'])
            self.assertEqual(create.call_count,1)
            self.assertEqual(server.call('routes',project='demo'),[])

    def test_real_stdio_protocol_initialize_create_name_and_fresh_reads(self):
        # Isolated executable exercises the actual JSONL transport and fresh
        # metadata clients, with a fixture standing in for the server store.
        root=Path(self.root);exe=root/'fake-codex';state=root/'state.json';log=root/'calls.jsonl'
        exe.write_text('#!'+sys.executable+'\n'+f'''
import json,sys
from pathlib import Path
assert sys.argv[1:]==['app-server','--stdio']
state=Path({str(state)!r});log=Path({str(log)!r})
for line in sys.stdin:
 q=json.loads(line)
 with log.open('a') as f:f.write(json.dumps(q)+'\\n')
 if 'id' not in q:continue
 m=q['method'];p=q['params']
 if m=='initialize':r={{'userAgent':'fixture'}}
 elif m=='permissionProfile/list':r={{'data':[{{'id':':danger-full-access','allowed':True}}]}}
 elif m=='model/list':r={{'data':[{{'model':'requested-model'}}]}}
 elif m=='thread/list':r={{'data':[],'nextCursor':None}}
 elif m=='thread/start':
  t={{'id':{self.sid!r},'cwd':{self.root!r},'ephemeral':False,'source':'vscode','status':{{'type':'idle'}},'name':None}}
  state.write_text(json.dumps(t));r={{'thread':t,'model':p['model'],'approvalPolicy':'never','activePermissionProfile':{{'id':':danger-full-access'}},'sandbox':{{'type':'dangerFullAccess'}}}}
 elif m=='thread/name/set':
  t=json.loads(state.read_text());t['name']=p['name'];state.write_text(json.dumps(t));r={{}}
 elif m=='thread/read':r={{'thread':json.loads(state.read_text())}}
 else:raise AssertionError(m)
 print(json.dumps({{'id':q['id'],'result':r}}),flush=True)
''');exe.chmod(0o700)
        with ModelDesktop() as desktop:
            result=self.open(desktop,title='Actual title',model='requested-model',timeout=2,
                             client_factory=functools.partial(sessions.CodexClient,executable=str(exe)))
        calls=[json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum(x.get('method')=='thread/start' for x in calls),1)
        self.assertEqual(sum(x.get('method')=='initialize' for x in calls),3)
        self.assertEqual(calls[0]['params']['clientInfo']['name'],'agent_bridge')
        self.assertTrue(calls[0]['params']['capabilities']['experimentalApi'])
        self.assertEqual(result['session']['name'],'Actual title')

    def test_model_catalog_pagination_and_repeated_cursor_refusal(self):
        class Catalog:
            def __init__(self,repeat=False):self.calls=[];self.repeat=repeat
            def call(self,method,params):
                self.calls.append(params)
                return {'data':[],'nextCursor':'again'} if self.repeat or len(self.calls)==1 else {'data':[{'model':'requested-model'}]}
        c=Catalog();codex_new.require_model(c,'requested-model')
        self.assertEqual(c.calls[-1]['cursor'],'again')
        with self.assertRaisesRegex(codex_new.CreationError,'repeated'):
            codex_new.require_model(Catalog(True),'requested-model')

    def switch(self, desktop, model='requested-model', **kwargs):
        return codex_new.set_model(self.sid, model, timeout=1, client_factory=self.factory,
            owner_factory=lambda **kw:codex_live.Client(desktop.path,**kw), **kwargs)

    def test_codex_model_sends_only_model_and_reads_back(self):
        with ModelDesktop(model='gpt-6-astra') as desktop:
            result = self.switch(desktop)
        updates = [x for x in desktop.calls if x['method']=='thread-follower-update-thread-settings']
        self.assertEqual(len(updates),1)
        self.assertEqual(updates[0]['params'],{'conversationId':self.sid,'threadSettings':{'model':'requested-model'}})
        self.assertEqual(updates[0]['targetClientId'],desktop.owner)
        self.assertEqual(sum(x['method']=='thread-follower-read-model-settings' for x in desktop.calls),2)
        self.assertFalse(any(x['method'].startswith(('thread-follower-steer','thread-follower-start'))
                             or x['method']=='thread-stream-following-changed' for x in desktop.calls))
        self.assertEqual((result['model'],result['previous_model'],result['changed']),('requested-model','gpt-6-astra',True))
        self.assertTrue(any(method=='model/list' for method,_,_ in self.calls))

    def test_codex_model_refuses_unadvertised_before_any_owner_call(self):
        with ModelDesktop() as desktop, self.assertRaisesRegex(codex_new.ModelSwitchError,'not advertised') as caught:
            self.switch(desktop,'missing')
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(desktop.calls,[])

    def test_codex_model_readback_mismatch_fails(self):
        for options in ({'keep_model':True},{'reject_update':True}):
            with self.subTest(options=options), ModelDesktop(model='gpt-6-astra',**options) as desktop, \
                    self.assertRaises(codex_new.ModelSwitchError) as caught:
                self.switch(desktop)
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(sum(x['method']=='thread-follower-update-thread-settings' for x in desktop.calls),1)

    def test_codex_model_never_retries_a_possibly_sent_mutation(self):
        with ModelDesktop(model='gpt-6-astra',drop_update=True) as desktop, \
                self.assertRaises(codex_new.ModelSwitchError) as caught:
            self.switch(desktop)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(sum(x['method']=='thread-follower-update-thread-settings' for x in desktop.calls),1)
        self.assertEqual(sum(x['method']=='initialize' for x in desktop.calls),1)

    def test_codex_model_cli_reports_result_and_failure(self):
        ok={'session':self.sid,'model':'requested-model','changed':True}
        with patch.object(codex_new,'set_model',return_value=ok) as switch:
            out=io.StringIO()
            with patch('sys.stdout',out):
                code=bridge.main(['codex-model',self.sid,'--model','requested-model','--state-dir',self.root])
        self.assertEqual((code,json.loads(out.getvalue())),(0,{'provider':'codex',**ok}))
        self.assertEqual(switch.call_args.args,(self.sid,'requested-model'))
        self.assertEqual(switch.call_args.kwargs,{'timeout':10})
        with patch.object(codex_new,'set_model',side_effect=codex_new.ModelSwitchError('lost',True)):
            out=io.StringIO()
            with patch('sys.stdout',out):
                code=bridge.main(['codex-model',self.sid,'--model','requested-model','--state-dir',self.root])
        self.assertEqual(code,1)
        self.assertEqual(json.loads(out.getvalue()),{'error':'lost','uncertain':True,'session':self.sid,'model':'requested-model'})

    def test_register_conflicts_are_refused(self):
        for key in ('session','discover'):
            args=argparse.Namespace(new=True,transport='codex',cwd=self.root,session=None,discover=False,title=None,model=None,timeout=1)
            setattr(args,key,self.sid if key=='session' else True)
            with self.assertRaisesRegex(ValueError,'cannot be combined'):
                bridge.registration(args)


if __name__=='__main__':unittest.main()
