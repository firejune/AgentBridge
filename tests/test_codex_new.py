import argparse
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import codex_live
import codex_new
import sessions
from support import RunningBroker
from test_codex_live import FakeDesktop


class NewCodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = str(Path(self.temp.name).resolve())
        self.old, self.sid = str(uuid.uuid4()), str(uuid.uuid4())
        self.urls = []

    def tearDown(self):
        self.temp.cleanup()

    def row(self, sid=None, source='vscode', cwd=None):
        return {'id':sid or self.sid,'provider':'codex','cwd':cwd or self.root,
                'source':source,'status':'unknown','runtime_observed':False,'name':'Worker'}

    def listing(self, rows=None):
        calls = []
        def read(cwd):
            calls.append(cwd)
            return [self.row(self.old)] if len(calls) == 1 else [self.row(self.old), *(rows if rows is not None else [self.row()])]
        return read

    def open(self, desktop, **kwargs):
        kwargs.setdefault('listing', self.listing())
        kwargs.setdefault('opener', self.urls.append)
        kwargs.setdefault('timeout', .08)
        return codex_new.open_codex(self.root, owner_factory=lambda **kw:codex_live.Client(desktop.path, **kw),
                                   poll=.005, **kwargs)

    def test_composer_url_quotes_unicode_path_and_first_message(self):
        url = codex_new.new_session_url('/tmp/folder & 한글', 'First & ? # message')
        parsed = urlparse(url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ('codex','threads','/new'))
        self.assertEqual(parse_qs(parsed.query), {'path':['/tmp/folder & 한글'],
                         'prompt':['First & ? # message'],'mode':['codex']})

    def test_success_identifies_exact_new_owned_desktop_session(self):
        with FakeDesktop() as desktop:
            result = self.open(desktop, title='Initial message')
            self.assertTrue(result['created'])
            self.assertEqual(result['session']['id'], self.sid)
            self.assertEqual(result['session']['cwd'], self.root)
            self.assertEqual(result['owner'], desktop.owner)
            self.assertEqual(result['prompt'], 'Initial message')
            self.assertEqual([x['method'] for x in desktop.calls], ['initialize','thread-owner-discovery'])
            self.assertEqual(desktop.calls[-1]['params']['conversationId'], self.sid)
            self.assertEqual(len(self.urls), 1)

    def test_default_prompt_only_waits_for_task(self):
        with FakeDesktop() as desktop:
            result = self.open(desktop)
        self.assertEqual(result['prompt'], codex_new.PROMPT)
        self.assertIn('Wait for a task', result['prompt'])

    def test_model_bad_timeout_missing_folder_refused_before_open(self):
        for kwargs in ({'model':'requested-model'}, {'model':''}, {'timeout':0},
                       {'timeout':float('nan')}, {'timeout':float('inf')}, {'title':' '}):
            with self.subTest(kwargs=kwargs), self.assertRaises(codex_new.CreationError):
                codex_new.open_codex(self.root, opener=self.urls.append, **kwargs)
        with self.assertRaises(codex_new.CreationError):
            codex_new.open_codex(self.root+'/missing', opener=self.urls.append)
        self.assertEqual(self.urls, [])

    def test_old_exec_subagent_and_other_workspace_do_not_become_target(self):
        bad = [self.row(source='exec'), self.row(str(uuid.uuid4()), source={'subAgent':{}}),
               self.row(str(uuid.uuid4()), cwd='/another/folder')]
        with FakeDesktop() as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop, listing=self.listing(bad), timeout=.02)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(desktop.calls, [])
        self.assertEqual(len(self.urls), 1)

    def test_two_new_sessions_refused_before_owner_selection(self):
        with FakeDesktop() as desktop, self.assertRaisesRegex(codex_new.CreationError, 'Multiple'):
            self.open(desktop, listing=self.listing([self.row(),self.row(str(uuid.uuid4()))]))
        self.assertEqual(desktop.calls, [])

    def test_no_owner_times_out_without_registering_or_starting_turn(self):
        with FakeDesktop('no-owner') as desktop, self.assertRaises(codex_new.CreationError) as caught:
            self.open(desktop, timeout=.02)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(caught.exception.session, self.sid)
        self.assertTrue(all(x['method'] in ('initialize','thread-owner-discovery') for x in desktop.calls))
        self.assertEqual(len(self.urls), 1)

    def test_explicit_owner_capability_required(self):
        with FakeDesktop('no-capability') as desktop, self.assertRaises(codex_new.CreationError):
            self.open(desktop, timeout=.02)

    def test_launcher_failure_is_not_retried(self):
        calls = []
        def fail(url):
            calls.append(url)
            raise OSError('failed')
        with self.assertRaises(codex_new.CreationError) as caught:
            codex_new.open_codex(self.root, listing=self.listing(), opener=fail)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(calls), 1)

    def test_launcher_timeout_preserves_uncertain_marker_without_retry(self):
        calls = []
        def timeout(url):
            calls.append(url)
            raise subprocess.TimeoutExpired(['open', url], 10)
        with self.assertRaises(codex_new.CreationError) as caught:
            codex_new.open_codex(self.root, listing=self.listing(), opener=timeout)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(calls), 1)

    def test_root_source_requires_exact_owner_not_source_label(self):
        with FakeDesktop() as desktop:
            result = self.open(desktop, listing=self.listing([self.row(source='cli')]))
            self.assertEqual(result['session']['id'], self.sid)
        with FakeDesktop('no-owner') as desktop, self.assertRaises(codex_new.CreationError):
            self.open(desktop, listing=self.listing([self.row(source='cli')]), timeout=.02)

    def test_listing_failure_before_open_does_not_launch(self):
        with self.assertRaises(ValueError):
            codex_new.open_codex(self.root, listing=lambda cwd:(_ for _ in ()).throw(ValueError('read failed')),
                                 opener=self.urls.append)
        self.assertEqual(self.urls, [])

    def test_saved_listing_never_claims_live_and_rejects_truncation(self):
        with patch.object(sessions,'list_codex',return_value={'sessions':[self.row(),self.row(source='exec')],'more_available':False}) as read:
            self.assertEqual(codex_new.desktop_rows(self.root), [self.row()])
            self.assertEqual(read.call_args.kwargs, {'cwd':self.root,'limit':10000,'mode':'saved'})
        with patch.object(sessions,'list_codex',return_value={'sessions':[],'more_available':True}):
            with self.assertRaisesRegex(codex_new.CreationError,'truncated'):
                codex_new.desktop_rows(self.root)

    def test_open_and_register_cli_use_same_creation_provider(self):
        result = {'created':True,'session':self.row(),'warning':None}
        with RunningBroker() as server, patch.object(codex_new,'open_codex',return_value=result) as create:
            for argv in (['open','--provider','codex'], ['register','worker','--project','demo','--transport','codex','--new']):
                out = io.StringIO()
                with patch('sys.stdout',out):
                    code = bridge.main(['--socket',server.path,*argv,'--cwd',self.root,'--title','First message'])
                self.assertEqual(code,0,out.getvalue())
            self.assertEqual(create.call_count, 2)
            self.assertEqual(create.call_args.kwargs['title'], 'First message')
            routes = server.call('routes',project='demo')
            self.assertEqual((routes[0]['transport'],routes[0]['session']), ('codex',self.sid))

    def test_uncertain_creation_reports_session_and_does_not_register(self):
        with RunningBroker() as server, patch.object(codex_new,'open_codex',side_effect=codex_new.CreationError('timeout',True,self.sid)) as create:
            out = io.StringIO()
            with patch('sys.stdout',out):
                code = bridge.main(['--socket',server.path,'register','worker','--project','demo',
                                    '--transport','codex','--new','--cwd',self.root])
            self.assertEqual(code,1)
            self.assertEqual(create.call_count,1)
            self.assertTrue(json.loads(out.getvalue())['uncertain'])
            self.assertEqual(server.call('routes',project='demo'), [])

    def test_registration_failure_keeps_created_session_recovery_id(self):
        result = {'created':True,'session':self.row(),'warning':None}
        with patch.object(codex_new,'open_codex',return_value=result) as create:
            out = io.StringIO()
            with patch('sys.stdout',out):
                code = bridge.main(['--socket',self.root+'/absent.sock','register','worker','--project','demo',
                                    '--transport','codex','--new','--cwd',self.root])
            output = json.loads(out.getvalue())
            self.assertEqual(code,1)
            self.assertTrue(output['created'])
            self.assertFalse(output['registered'])
            self.assertEqual(output['session'],self.sid)
            self.assertEqual(create.call_count,1)

    def test_register_new_rejects_existing_target_flags(self):
        for key in ('session','discover'):
            args = argparse.Namespace(new=True,transport='codex',cwd=self.root,session=None,
                                      discover=False,title=None,model=None,timeout=1)
            setattr(args,key,self.sid if key=='session' else True)
            with self.assertRaisesRegex(ValueError,'cannot be combined'):
                bridge.registration(args)


if __name__ == '__main__':
    unittest.main()
