import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
import wt

GIT_ENV = {'GIT_AUTHOR_NAME':'t','GIT_AUTHOR_EMAIL':'t@example.invalid','GIT_COMMITTER_NAME':'t',
           'GIT_COMMITTER_EMAIL':'t@example.invalid','GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1'}


def run(cwd, *args):
    return subprocess.run(['git','-C',str(cwd),*args],check=True,stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,text=True).stdout.strip()


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ,GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        for name in ('CLAUDE_CODE_SESSION_ID','CODEX_THREAD_ID',wt.ROOT_ENV,wt.WORKSPACE_ENV):
            os.environ.pop(name,None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.origin = base/'origin.git'
        self.ws = base/'ws'
        self.root = self.ws/'.worktrees'
        self.repo = self.ws/'proj'
        self.ws.mkdir()
        run(base,'init','-q','--bare','-b','main',str(self.origin))
        run(base,'clone','-q',str(self.origin),str(self.repo))
        (self.repo/'a.txt').write_text('one\n')
        run(self.repo,'add','a.txt');run(self.repo,'commit','-qm','init');run(self.repo,'push','-q','origin','main')
        run(self.repo,'remote','set-head','origin','main')
        self.pulls = {}

    def new(self, agent='claude', issue='7', session=None, **kwargs):
        return wt.new('proj',agent,issue,slug='x',session=session or str(uuid.uuid4()),provider='claude',
                      workspace=self.ws,root=self.root,**kwargs)

    def commit(self, path, name='b.txt', text='two\n'):
        (Path(path)/name).write_text(text)
        run(path,'add',name);run(path,'commit','-qm',f'change {name}')

    def land_squash(self, path, name='b.txt', text='two\n'):
        """Put the same content on main as a separate commit, the way a squash merge does."""
        run(self.repo,'checkout','-q','main')
        (self.repo/name).parent.mkdir(parents=True,exist_ok=True)
        (self.repo/name).write_text(text)
        run(self.repo,'add',name);run(self.repo,'commit','-qm','squash');run(self.repo,'push','-q','origin','main')
        run(self.repo,'fetch','-q','origin')

    def sweep(self, apply=False, live=(), holders=(), **kwargs):
        return wt.sweep(apply,workspace=self.ws,root=self.root,live=list(live),
                        holders=None if holders is None else list(holders),
                        pulls_for=lambda repo: self.pulls,**kwargs)

    def row(self, report, path):
        return next(r for r in report['worktrees'] if r['path'] == str(path))

    def test_new_writes_owner_outside_the_tree_and_refuses_existing_path(self):
        created = self.new(session='s-1')
        path = Path(created['path'])
        self.assertEqual(path,self.root/'proj'/'claude-7')
        self.assertEqual(created['branch'],'claude/7-x')
        self.assertEqual(run(path,'branch','--show-current'),'claude/7-x')
        owner = json.loads(wt.owner_path(path).read_text())
        self.assertEqual({k:owner[k] for k in ('agent','session','issue','repo','purpose')},
                         {'agent':'claude','session':'s-1','issue':'7','repo':'proj','purpose':'work'})
        self.assertIn('created',owner)
        self.assertFalse((path/wt.OWNER_FILE).exists())
        self.assertEqual(run(path,'status','--porcelain','--untracked-files=all'),'')
        with self.assertRaisesRegex(wt.WorktreeError,'already exists'):
            self.new()

    def test_new_detached_pin_has_no_branch(self):
        created = self.new(issue='pin',detach='origin/main')
        self.assertIsNone(created['branch'])
        self.assertEqual(created['owner']['purpose'],'pin')
        self.assertEqual(run(created['path'],'rev-parse','--abbrev-ref','HEAD'),'HEAD')

    def test_new_takes_the_calling_session_from_the_environment(self):
        with mock.patch.dict(os.environ,{'CLAUDE_CODE_SESSION_ID':'env-session'}):
            created = wt.new('proj','claude','9',slug='',workspace=self.ws,root=self.root)
        self.assertEqual((created['owner']['provider'],created['owner']['session']),('claude','env-session'))
        self.assertEqual(created['branch'],'claude/9')

    def test_squash_merged_clean_dead_owner_is_removed_with_its_branch(self):
        path = Path(self.new()['path'])
        self.commit(path)
        self.land_squash(path)
        self.assertNotEqual(subprocess.run(['git','-C',str(path),'merge-base','--is-ancestor','HEAD','origin/main']).returncode,0)
        dry = self.sweep()
        self.assertEqual(self.row(dry,path)['action'],'remove',self.row(dry,path)['reasons'])
        self.assertTrue(path.exists())
        applied = self.sweep(apply=True)
        self.assertTrue(self.row(applied,path)['result']['removed'])
        self.assertFalse(path.exists())
        self.assertEqual(run(self.repo,'branch','--list','claude/7-x'),'')

    def test_merged_pr_is_landed_even_when_main_moved_on(self):
        path = Path(self.new()['path'])
        self.commit(path)
        self.land_squash(path,text='rewritten later\n')
        self.pulls = {'claude/7-x':[{'number':3,'state':'MERGED','url':'u','headRefName':'claude/7-x'}]}
        row = self.row(self.sweep(),path)
        self.assertEqual((row['action'],row['landed_by']),('remove','PR #3 merged'))

    def test_keeps_dirty(self):
        path = Path(self.new()['path'])
        (path/'scratch.log').write_text('x')
        row = self.row(self.sweep(apply=True),path)
        self.assertEqual(row['action'],'keep')
        self.assertTrue(any(r.startswith('dirty') for r in row['reasons']))
        self.assertTrue(path.exists())

    def test_keeps_unmerged(self):
        path = Path(self.new()['path'])
        self.commit(path)
        row = self.row(self.sweep(apply=True),path)
        self.assertEqual(row['action'],'keep')
        self.assertIn('not landed: changed paths differ from origin/main',row['reasons'])
        self.assertTrue(path.exists())

    def test_keeps_open_and_closed_unmerged_prs(self):
        path = Path(self.new()['path'])
        for state,reason in (('OPEN','PR #4 is open'),('CLOSED','PR #4 was closed without merging')):
            self.pulls = {'claude/7-x':[{'number':4,'state':state,'url':'u','headRefName':'claude/7-x'}]}
            row = self.row(self.sweep(apply=True),path)
            self.assertEqual(row['action'],'keep')
            self.assertIn(reason,row['reasons'])

    def test_keeps_when_owner_alive(self):
        sid = str(uuid.uuid4())
        path = Path(self.new(session=sid)['path'])
        live = [{'provider':'claude','id':sid,'cwd':'/elsewhere','state':'alive'}]
        row = self.row(self.sweep(apply=True,live=live),path)
        self.assertEqual(row['action'],'keep')
        self.assertIn(f'owner session claude:{sid} is alive',row['reasons'])
        self.assertTrue(path.exists())

    def test_keeps_when_a_live_session_cwd_is_inside(self):
        path = Path(self.new()['path'])
        (path/'sub').mkdir()
        live = [{'provider':'codex','id':'other','cwd':str(path/'sub'),'state':'unknown'}]
        row = self.row(self.sweep(apply=True,live=live),path)
        self.assertEqual(row['action'],'keep')
        self.assertEqual(row['in_use_by'][0]['id'],'other')
        self.assertTrue(path.exists())

    @unittest.skipUnless(shutil.which('lsof'),'lsof is not installed')
    def test_keeps_when_a_process_holds_a_file_below_it(self):
        path = Path(self.new()['path'])
        (path/'deep').mkdir()
        (path/'deep'/'held.txt').write_text('x')
        run(path,'add','deep');run(path,'commit','-qm','held')
        self.land_squash(path,'deep/held.txt','x')
        self.assertEqual(self.row(self.sweep(),path)['action'],'remove')
        holder = subprocess.Popen([sys.executable,'-c','import sys,time;f=open(sys.argv[1]);print("ready",flush=True);time.sleep(60)',
                                   str(path/'deep'/'held.txt')],stdout=subprocess.PIPE,text=True,cwd='/')
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(),'ready')
        row = self.row(self.sweep(apply=True,holders=None),path)
        self.assertEqual(row['action'],'keep')
        self.assertIn(holder.pid,[h['pid'] for h in row['open_by']])
        self.assertTrue(path.exists())

    def test_keeps_unowned_until_adopted(self):
        path = self.root/'proj'/'legacy'
        run(self.repo,'worktree','add','-q','--detach',str(path),'origin/main')
        row = self.row(self.sweep(),path)
        self.assertEqual(row['action'],'keep')
        self.assertIn('no OWNER.json; adopt it to make it sweepable',row['reasons'])
        wt.adopt(path,'codex','12',session='dead-session')
        self.assertEqual(self.row(self.sweep(),path)['action'],'remove')
        with self.assertRaisesRegex(wt.WorktreeError,'already has an owner'):
            wt.adopt(path,'codex','12')
        with self.assertRaisesRegex(wt.WorktreeError,'main checkout'):
            wt.adopt(self.repo,'codex','12')

    def test_harness_location_is_read_but_never_removed(self):
        path = self.repo/'.claude'/'worktrees'/'h1'
        run(self.repo,'worktree','add','-q','--detach',str(path),'origin/main')
        wt.adopt(path,'claude','1',session='dead')
        row = self.row(self.sweep(apply=True),path)
        self.assertEqual((row['location'],row['action']),('harness','keep'))
        self.assertTrue(path.exists())

    def test_clones_are_reported_never_removed(self):
        clone = self.ws/'proj-copy'
        run(self.ws,'clone','-q',str(self.origin),str(clone))
        report = self.sweep(apply=True)
        self.assertEqual(report['clones'],[{'path':str(clone),'of':str(self.repo),'origin':str(self.origin).lower().rstrip('/').removesuffix('.git')}])
        self.assertTrue(clone.exists())
        self.assertNotIn(str(clone),[r['path'] for r in report['worktrees']])

    def test_discovery_failure_blocks_removal(self):
        path = Path(self.new()['path'])
        with mock.patch.object(wt,'live_sessions',return_value=([],{'codex':'app-server down'})):
            report = wt.sweep(True,workspace=self.ws,root=self.root,holders=[],pulls_for=lambda repo: {})
        row = self.row(report,path)
        self.assertEqual(row['action'],'keep')
        self.assertIn('codex session discovery failed',row['reasons'])
        self.assertEqual(report['errors'],{'codex':'app-server down'})

    def test_list_flags_a_main_checkout_off_the_base_branch(self):
        main = lambda: self.row(wt.survey(workspace=self.ws,root=self.root,live=[],holders=[],
                                          pulls_for=lambda repo: {}),self.repo)
        self.assertEqual(main()['reasons'],['main checkout'])
        run(self.repo,'checkout','-qb','feature')
        self.assertEqual(main()['reasons'],['main checkout','main checkout is on feature, not main'])
        self.assertEqual(main()['action'],'keep')

    def test_remove_rechecks_cleanliness(self):
        path = Path(self.new()['path'])
        row = self.row(self.sweep(),path)
        self.assertEqual(row['action'],'remove')
        (path/'late.txt').write_text('x')
        self.assertEqual(wt.remove(row),{'removed':False,'reason':'became dirty'})
        self.assertTrue(path.exists())

    def test_bridge_cli_routes_wt(self):
        self.new()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), \
                mock.patch.object(wt,'live_sessions',return_value=([],{})), \
                mock.patch.object(wt,'open_files',return_value=[]), \
                mock.patch.object(wt,'gh_pulls',return_value={}):
            code = bridge.main(['wt','sweep','--workspace',str(self.ws),'--root',str(self.root)])
        report = json.loads(buffer.getvalue())
        self.assertEqual(code,0)
        self.assertFalse(report['applied'])
        self.assertEqual(report['summary']['removable'],1)
        self.assertEqual(report['summary']['removed'],0)
        self.assertEqual(report['summary']['keep'],0)


if __name__ == '__main__':
    unittest.main()
