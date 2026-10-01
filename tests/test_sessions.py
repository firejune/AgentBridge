import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
import sessions
from support import RunningBroker


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.sid, self.other = str(uuid.uuid4()), str(uuid.uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def fake_codex(self, fail_proxy=False, hang=False, repeat_cursor=False):
        exe = self.root / 'codex'
        rows = [{'id':sid,'sessionId':str(uuid.uuid4()),'name':name,'cwd':str(self.root),
                 'status':{'type':'active'},'source':'cli','updatedAt':1}
                for sid,name in ((self.sid,'UI commander'),(self.other,'Logic commander'))]
        exe.write_text('#!'+sys.executable+'\n'+f'''
import sys,json,time
from pathlib import Path
rows = {rows!r}
if {fail_proxy!r} and sys.argv[-1] == 'proxy':
 print('No shared socket',file=sys.stderr);sys.exit(1)
for line in sys.stdin:
 request=json.loads(line)
 with Path({str(self.root/'requests.jsonl')!r}).open('a') as log:
  log.write(json.dumps(request)+'\\n')
 if 'id' not in request:continue
 if {hang!r}:time.sleep(10)
 method=request['method'];params=request['params']
 if method=='initialize':result={{'userAgent':'fake'}}
 elif method=='thread/list':
  page=0 if not params.get('cursor') else 1
  result={{'data':[rows[page]],'nextCursor':'page-2' if page==0 or {repeat_cursor!r} else None}}
 elif method=='thread/read':
  item=next((row for row in rows if row['id']==params['threadId']),None)
  result={{'thread':item}}
 else:raise RuntimeError('Unexpected mutating method: '+method)
 print(json.dumps({{'method':'notification'}}))
 print(json.dumps({{'id':request['id'],'result':result}}),flush=True)
''')
        exe.chmod(0o700)
        return str(exe)

    def fake_claude(self):
        exe = self.root/'claude'
        rows = [{'sessionId':self.sid,'name':'View commander','cwd':str(self.root),'kind':'interactive','status':'busy','pid':12},
                {'sessionId':self.other,'name':'Elsewhere','cwd':'/another/project','kind':'background','status':'idle'}]
        exe.write_text('#!'+sys.executable+'\nimport sys,json\n'
                       "assert sys.argv[1:]==['agents','--json','--all']\n"
                       f'print({json.dumps(rows)!r})\n')
        exe.chmod(0o700)
        return str(exe)

    def test_codex_pagination_uses_thread_id_and_metadata_only(self):
        result=sessions.list_codex(self.fake_codex(),mode='shared')
        self.assertEqual([x['id'] for x in result['sessions']],[self.sid,self.other])
        self.assertFalse(result['more_available'])
        self.assertEqual(result['sessions'][0]['status'],'active')
        calls=[json.loads(x) for x in (self.root/'requests.jsonl').read_text().splitlines()]
        self.assertEqual([x['method'] for x in calls],['initialize','initialized','thread/list','thread/list'])
        self.assertTrue(calls[-1]['params']['useStateDbOnly'])
        self.assertEqual(calls[-1]['params']['cursor'],'page-2')

    def test_saved_fallback_does_not_claim_live_status(self):
        result=sessions.list_codex(self.fake_codex(fail_proxy=True),limit=1)
        self.assertIsNotNone(result['warning'])
        self.assertEqual(result['sessions'][0]['status'],'unknown')
        self.assertFalse(result['sessions'][0]['runtime_observed'])
        self.assertTrue(result['more_available'])

    def test_shared_only_does_not_silently_fallback(self):
        with self.assertRaises(sessions.DiscoveryError):
            sessions.list_codex(self.fake_codex(fail_proxy=True),mode='shared')

    def test_codex_exact_read_never_loads_turns(self):
        result=sessions.read_codex(self.sid,self.fake_codex(),mode='saved')
        self.assertEqual(result['session']['id'],self.sid)
        calls=[json.loads(x) for x in (self.root/'requests.jsonl').read_text().splitlines()]
        self.assertEqual(calls[-1]['params'],{'threadId':self.sid,'includeTurns':False})

    def test_repeated_cursor_fails(self):
        with self.assertRaisesRegex(sessions.DiscoveryError,'repeated'):
            sessions.list_codex(self.fake_codex(repeat_cursor=True),mode='shared')

    def test_timeout_is_bounded(self):
        with self.assertRaisesRegex(sessions.DiscoveryError,'timed out'):
            sessions.CodexClient(self.fake_codex(hang=True),mode='shared',timeout=0.05)

    def test_claude_filters_interactive_cwd_and_name(self):
        result=sessions.list_claude(self.fake_claude(),cwd=str(self.root),search='VIEW')
        self.assertEqual(len(result['sessions']),1)
        self.assertEqual(result['sessions'][0]['status'],'busy')
        self.assertEqual(result['sessions'][0]['id'],self.sid)

    def test_absent_claude_session_does_not_choose_another(self):
        with self.assertRaisesRegex(sessions.DiscoveryError,'absent or ambiguous'):
            sessions.inspect_session('claude',str(uuid.uuid4()),self.fake_claude())

    def test_login_shell_terminal_controls_are_not_json_content(self):
        self.assertEqual(sessions.cli_json('\x1b(B\x1b[m[{"status":"busy"}]'),[{'status':'busy'}])
        with self.assertRaises(ValueError):
            sessions.cli_json('unexpected banner\n[{"status":"busy"}]')

    def test_partial_failure_keeps_successful_provider(self):
        result=sessions.discover(codex_executable=str(self.root/'missing'),claude_executable=self.fake_claude())
        self.assertIn('codex',result['errors'])
        self.assertEqual(len(result['providers']['claude']['sessions']),2)

    def test_discovered_registration_infers_path_and_rejects_mismatch(self):
        with RunningBroker() as server:
            exe=self.fake_codex()
            args=[sys.executable,bridge.__file__,'--socket',server.path,'register','naru',
                  '--project','demo','--transport','codex','--session',self.sid,
                  '--discover','--executable',exe,'--codex-mode','saved']
            result=subprocess.run(args,capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout)['cwd'],str(self.root))
            failed=subprocess.run([*args,'--cwd','/another/path'],capture_output=True,text=True)
            self.assertEqual(failed.returncode,1)
            self.assertIn('differs',failed.stdout)
            self.assertEqual(server.call('routes',project='demo')[0]['cwd'],str(self.root))

    def test_session_list_does_not_require_broker(self):
        socketpath=self.root/'must-not-exist.sock'
        result=subprocess.run([sys.executable,bridge.__file__,'--socket',str(socketpath),'sessions',
             '--provider','claude','--claude-executable',self.fake_claude()],capture_output=True,text=True,check=True)
        self.assertEqual(len(json.loads(result.stdout)['providers']['claude']['sessions']),2)
        self.assertFalse(socketpath.exists())


if __name__=='__main__':
    unittest.main()
