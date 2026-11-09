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
        for name in ('CLAUDE_CODE_SESSION_ID','CODEX_THREAD_ID',wt.ROOT_ENV,wt.WORKSPACE_ENV,wt.LINKS_ENV):
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
        self.pulls = {'claude/7-x':[{'number':3,'state':'MERGED','url':'u','headRefName':'claude/7-x',
                                     'headRefOid':run(path,'rev-parse','HEAD')}]}
        row = self.row(self.sweep(),path)
        self.assertEqual((row['action'],row['landed_by']),('remove','PR #3 merged at this HEAD'))

    def test_keeps_commits_made_after_the_pr_merged(self):
        path = Path(self.new()['path'])
        self.commit(path)
        merged_head = run(path,'rev-parse','HEAD')
        self.land_squash(path)
        self.commit(path,'later.txt','follow-up\n')
        self.pulls = {'claude/7-x':[{'number':3,'state':'MERGED','url':'u','headRefName':'claude/7-x',
                                     'headRefOid':merged_head}]}
        row = self.row(self.sweep(apply=True),path)
        self.assertEqual(row['action'],'keep')
        self.assertIn('not landed: changed paths differ from origin/main',row['reasons'])
        self.assertTrue(path.exists())

    def test_keeps_unlanded_files_with_unusual_names(self):
        for i,name in enumerate(('café.txt','with space.txt','quo"te.txt','star*.txt')):
            with self.subTest(name=name):
                path = Path(self.new(issue=f'n{i}')['path'])
                self.commit(path,name,'unlanded\n')
                row = self.row(self.sweep(apply=True),path)
                self.assertEqual(row['action'],'keep')
                self.assertIn('not landed: changed paths differ from origin/main',row['reasons'])
                self.assertTrue(path.exists())

    def test_landed_files_with_unusual_names_are_removable(self):
        path = Path(self.new()['path'])
        self.commit(path,'café q"*.txt','same\n')
        self.land_squash(path,'café q"*.txt','same\n')
        self.assertEqual(self.row(self.sweep(),path)['action'],'remove')

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

    def _setup_linked_workspace(self):
        base = Path(self.temp.name).resolve()
        bin_dir = base / 'stub-bin'
        bin_dir.mkdir(exist_ok=True)
        stub_bun = bin_dir / 'bun'
        stub_bun.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, pathlib, sys\n'
            'if sys.argv[1:2] != ["install"]:\n'
            '    sys.exit(2)\n'
            'cwd = pathlib.Path.cwd()\n'
            'mode = os.environ.get("STUB_BUN_MODE", "ok")\n'
            'if mode == "fail":\n'
            '    sys.stderr.write("stub bun install failure\\n")\n'
            '    sys.exit(1)\n'
            'bunfig = (cwd / "bunfig.toml").read_text(encoding="utf-8")\n'
            'assert \'linker = "isolated"\' in bunfig, bunfig\n'
            'pkg = json.loads((cwd / "package.json").read_text(encoding="utf-8"))\n'
            '(cwd / "bun.lock").write_text("isolated-sidecar-lock\\n", encoding="utf-8")\n'
            'store = cwd / "node_modules" / ".bun"\n'
            'store.mkdir(parents=True, exist_ok=True)\n'
            'members = {}\n'
            'for rel in pkg["workspaces"]:\n'
            '    mp = (cwd / rel).resolve()\n'
            '    mp_pkg = json.loads((mp / "package.json").read_text(encoding="utf-8"))\n'
            '    members[mp_pkg["name"]] = (rel, mp, mp_pkg)\n'
            'for name, (rel, mp, mp_pkg) in members.items():\n'
            '    nm = mp / "node_modules"\n'
            '    nm.mkdir(parents=True, exist_ok=True)\n'
            '    for sec in ("dependencies", "devDependencies"):\n'
            '        for dep in (mp_pkg.get(sec) or {}):\n'
            '            target_dir = nm / dep\n'
            '            target_dir.parent.mkdir(parents=True, exist_ok=True)\n'
            '            if dep in members:\n'
            '                if mode == "escape_main" and rel.startswith("../"):\n'
            '                    main_target = pathlib.Path(os.environ["STUB_MAIN_ESCAPE"])\n'
            '                    target_dir.symlink_to(main_target)\n'
            '                elif mode == "registry_fallback" and rel.startswith("../"):\n'
            '                    reg = store / (dep.replace("/", "+") + "@registry")\n'
            '                    reg.mkdir(parents=True, exist_ok=True)\n'
            '                    (reg / "package.json").write_text(json.dumps({"name": dep}), encoding="utf-8")\n'
            '                    target_dir.symlink_to(reg)\n'
            '                elif mode == "copy_fallback" and rel.startswith("../"):\n'
            '                    target_dir.mkdir(parents=True, exist_ok=True)\n'
            '                    (target_dir / "package.json").write_text(json.dumps({"name": dep}), encoding="utf-8")\n'
            '                elif mode == "wrong_peer" and rel.startswith("../") and dep == "@synth/play":\n'
            '                    target_dir.symlink_to(members["@synth/core"][1])\n'
            '                else:\n'
            '                    target_dir.symlink_to(members[dep][1])\n'
            '            else:\n'
            '                ext = store / dep.replace("/", "+")\n'
            '                ext.mkdir(parents=True, exist_ok=True)\n'
            '                (ext / "index.js").write_text("module.exports = 1;\\n", encoding="utf-8")\n'
            '                target_dir.symlink_to(ext)\n'
            '    if mode == "dirty_lane" and rel.startswith("../"):\n'
            '        (mp / "package.json").write_text("{}\\n", encoding="utf-8")\n',
            encoding='utf-8',
        )
        stub_bun.chmod(0o755)
        os.environ['PATH'] = f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}'

        specs = {
            'rigc': {
                'pkg': {
                    'name': '@synth/core',
                    'version': '3.6.0',
                    'exports': {'.': './src/index.ts'},
                    'devDependencies': {'spine-html': '^0.4.1'},
                },
                'files': {
                    'src/index.ts': 'export const CORE = "3.6.0";\n',
                    'bench/heavy.bin': 'exclude-me\n',
                },
            },
            'rig-parts': {
                'pkg': {
                    'name': '@synth/parts',
                    'version': '2.36.0',
                    'dependencies': {'@synth/core': '3.6.0'},
                },
                'files': {'src/index.ts': 'export const PARTS = "2.36.0";\n'},
            },
            'rig-play': {
                'pkg': {
                    'name': '@synth/play',
                    'version': '1.23.0',
                    'exports': {
                        '.': {
                            '@synth/source': './src/index.ts',
                            'types': './dist/index.d.ts',
                            'import': './dist/index.js',
                        }
                    },
                    'dependencies': {'@synth/core': '3.6.0', '@synth/parts': '2.36.0'},
                },
                'files': {'src/index.ts': 'export const PLAY = "1.23.0";\n'},
            },
            'rig-review': {
                'pkg': {
                    'name': '@synth/review',
                    'version': '2.8.0',
                    'dependencies': {'@synth/play': '1.23.0'},
                    'devDependencies': {'@synth/parts': '2.36.0'},
                },
                'files': {'src/index.ts': 'export const REVIEW = "2.8.0";\n'},
            },
            'rig-create': {
                'pkg': {
                    'name': '@synth/create',
                    'version': '0.0.0',
                    'private': True,
                    'dependencies': {'@synth/core': '3.6.0', '@synth/review': '2.8.0'},
                },
                'files': {'src/index.ts': 'export const CREATE = "0.0.0";\n'},
            },
        }
        repos, shas, main_snapshots = {}, {}, {}
        for name, item in specs.items():
            origin = base / f'{name}.git'
            checkout = self.ws / name
            run(base, 'init', '-q', '--bare', '-b', 'main', str(origin))
            run(base, 'clone', '-q', str(origin), str(checkout))
            (checkout / '.gitignore').write_text('node_modules/\n', encoding='utf-8')
            (checkout / 'package.json').write_text(json.dumps(item['pkg'], indent=2) + '\n', encoding='utf-8')
            (checkout / 'bun.lock').write_text(f'frozen-lock-{name}\n', encoding='utf-8')
            for rel, content in item['files'].items():
                fp = checkout / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content, encoding='utf-8')
            run(checkout, 'add', '.')
            run(checkout, 'commit', '-qm', f'init {name}')
            run(checkout, 'push', '-q', 'origin', 'main')
            run(checkout, 'remote', 'set-head', 'origin', 'main')
            shas[name] = run(checkout, 'rev-parse', 'HEAD')
            # Populate main checkout node_modules to verify inode/target preservation
            nm_sentinel = checkout / 'node_modules' / '@synth' / 'registry-pin'
            nm_sentinel.mkdir(parents=True, exist_ok=True)
            marker = nm_sentinel / 'pin.js'
            marker.write_text(f'registry-{name}\n', encoding='utf-8')
            link = checkout / 'node_modules' / 'active-link'
            link.symlink_to(marker)
            repos[name] = checkout
            main_snapshots[name] = self._snapshot_main_state(checkout)

        links_cfg = {
            'schema': 'agent-bridge/wt-links@1',
            'condition': '@synth/source',
            'linker': 'isolated',
            'members': {
                'rigc': {
                    'path': 'rigc',
                    'name': '@synth/core',
                    'version': '3.6.0',
                    'ref': 'origin/main',
                    'legacy_names': ['rig-c', 'spine-rigc'],
                },
                'rig-parts': {
                    'path': 'rig-parts',
                    'name': '@synth/parts',
                    'version': '2.36.0',
                    'ref': 'origin/main',
                },
                'rig-play': {
                    'path': 'rig-play',
                    'name': '@synth/play',
                    'version': '1.23.0',
                    'ref': 'origin/main',
                    'requires_source_condition': True,
                },
                'rig-review': {
                    'path': 'rig-review',
                    'name': '@synth/review',
                    'version': '2.8.0',
                    'ref': 'origin/main',
                },
                'rig-create': {
                    'path': 'rig-create',
                    'name': '@synth/create',
                    'version': '0.0.0',
                    'ref': 'origin/main',
                    'private': True,
                    'leaf': True,
                },
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / wt.LINKS_FILE).write_text(json.dumps(links_cfg, indent=2) + '\n', encoding='utf-8')
        return repos, shas, main_snapshots, links_cfg

    def _snapshot_main_state(self, checkout):
        snap = {}
        for root_dir, dirs, files in os.walk(checkout, topdown=True, followlinks=False):
            if '.git' in dirs:
                dirs.remove('.git')
            rp = Path(root_dir)
            for entry_name in [*dirs, *files]:
                p = rp / entry_name
                rel = str(p.relative_to(checkout))
                st = os.lstat(p)
                target = os.readlink(p) if p.is_symlink() else None
                snap[rel] = (st.st_dev, st.st_ino, st.st_mode, target)
        return snap

    def test_new_with_links_config_creates_isolated_sidecar_and_preserves_main_checkouts(self):
        repos, shas, main_snapshots, _ = self._setup_linked_workspace()
        # Unconfigured repo (proj) keeps existing behavior with no sidecar
        plain = self.new(issue='1')
        self.assertNotIn('sidecar', plain)
        self.assertFalse(wt.sidecar_path(plain['path']).exists())

        # Configured repo (rig-review) provisions isolated .ws sidecar and omits leaf rig-create
        created = wt.new('rig-review', 'jinni', '110', slug='link', session='s-110', provider='agy',
                         workspace=self.ws, root=self.root)
        lane = Path(created['path'])
        ws_path = Path(created['sidecar'])
        self.assertEqual(ws_path, self.root / 'rig-review' / 'jinni-110.ws')
        self.assertTrue(ws_path.is_dir())
        self.assertEqual(created['condition'], '@synth/source')
        self.assertEqual(created['source_command'], 'bun --conditions=@synth/source')
        self.assertEqual(set(created['linked_peers']), {'rigc', 'rig-parts', 'rig-play'})
        for peer_key in ('rigc', 'rig-parts', 'rig-play'):
            self.assertEqual(created['linked_peers'][peer_key]['commit'], shas[peer_key])
            peer_src = ws_path / 'peers' / peer_key / 'src' / 'index.ts'
            self.assertTrue(peer_src.is_file())
            # Realpath stays inside .ws/peers/<sibling>, never main checkout
            self.assertTrue(wt.inside(peer_src.resolve(), (ws_path / 'peers' / peer_key).resolve()))
            self.assertEqual(os.lstat(peer_src).st_mode & 0o222, 0)
        # Full committed tree is materialized and external legacy spine-html pin remains intact
        self.assertTrue((ws_path / 'peers' / 'rigc' / 'bench' / 'heavy.bin').is_file())
        core_peer_pkg = json.loads((ws_path / 'peers' / 'rigc' / 'package.json').read_text(encoding='utf-8'))
        self.assertEqual(core_peer_pkg['devDependencies']['spine-html'], '^0.4.1')
        self.assertTrue((ws_path / 'peers' / 'rigc' / 'node_modules' / 'spine-html' / 'index.js').is_file())

        # Transitive link resolution stays within sidecar peers
        review_play = (lane / 'node_modules' / '@synth' / 'play' / 'src' / 'index.ts').resolve()
        play_parts = (ws_path / 'peers' / 'rig-play' / 'node_modules' / '@synth' / 'parts' / 'src' / 'index.ts').resolve()
        self.assertEqual(review_play, (ws_path / 'peers' / 'rig-play' / 'src' / 'index.ts').resolve())
        self.assertEqual(play_parts, (ws_path / 'peers' / 'rig-parts' / 'src' / 'index.ts').resolve())

        # Primary lane tracked manifest and lockfile are untouched and worktree is clean
        self.assertEqual(wt.dirty_count(lane), 0)
        self.assertEqual((lane / 'bun.lock').read_text(encoding='utf-8'), 'frozen-lock-rig-review\n')

        # Main checkouts' node_modules inodes, targets, and working trees are 100% unchanged
        for name, checkout in repos.items():
            self.assertEqual(self._snapshot_main_state(checkout), main_snapshots[name], f'main mutated in {name}')

        # Fixed-source provenance: advancing shared main checkout later does not mutate existing sidecar snapshot
        (repos['rig-play'] / 'src' / 'index.ts').write_text('export const PLAY = "1.23.0-live-edit";\n', encoding='utf-8')
        self.assertIn('"1.23.0"', (ws_path / 'peers' / 'rig-play' / 'src' / 'index.ts').read_text(encoding='utf-8'))
        (repos['rig-play'] / 'src' / 'index.ts').write_text('export const PLAY = "1.23.0";\n', encoding='utf-8')

        # Paired branch override (--peer rig-play=<branch>) materializes the exact branch commit
        run(repos['rig-play'], 'checkout', '-qb', 'pair/play-branch')
        (repos['rig-play'] / 'src' / 'index.ts').write_text('export const PLAY = "1.23.0-paired";\n', encoding='utf-8')
        run(repos['rig-play'], 'add', 'src/index.ts')
        run(repos['rig-play'], 'commit', '-qm', 'paired change')
        paired_sha = run(repos['rig-play'], 'rev-parse', 'HEAD')
        run(repos['rig-play'], 'checkout', '-q', 'main')

        paired = wt.new('rig-create', 'jinni', '111', slug='pair', session='s-111', provider='agy',
                        workspace=self.ws, root=self.root, peers=['rig-play=pair/play-branch'])
        self.assertEqual(paired['linked_peers']['rig-play']['commit'], paired_sha)
        self.assertIn('1.23.0-paired', (Path(paired['sidecar']) / 'peers' / 'rig-play' / 'src' / 'index.ts').read_text(encoding='utf-8'))

    def test_new_sidecar_named_failures_and_transactional_rollback(self):
        repos, _, main_snapshots, links_cfg = self._setup_linked_workspace()

        def assert_rolled_back(repo_name, agent, issue, branch_prefix):
            lane = self.root / repo_name / f'{agent}-{issue}'
            ws_p = wt.sidecar_path(lane)
            self.assertFalse(lane.exists())
            self.assertFalse(ws_p.exists())
            self.assertEqual(run(repos[repo_name], 'branch', '--list', f'{branch_prefix}*'), '')

        # 1. WT_LINK_SIDECAR_EXISTS
        preexisting_ws = self.root / 'rig-review' / 'jinni-201.ws'
        preexisting_ws.mkdir(parents=True, exist_ok=True)
        with self.assertRaisesRegex(wt.WorktreeError, 'WT_LINK_SIDECAR_EXISTS'):
            wt.new('rig-review', 'jinni', '201', slug='x', workspace=self.ws, root=self.root)
        self.assertFalse((self.root / 'rig-review' / 'jinni-201').exists())
        shutil.rmtree(preexisting_ws)

        # 2. WT_LINK_PROTECTED_MAIN_REFUSED (config key traversal/absolute/alias escape, --peer checkout path, or escaping symlink)
        for idx, bad_key in enumerate((str(repos['rigc']), '../../../rigc', 'alias-rigc'), start=1):
            escape_cfg = json.loads(json.dumps(links_cfg))
            escape_cfg['members'][bad_key] = {
                'path': str(repos['rigc']),
                'name': '@synth/escaped',
                'version': '0.0.1',
                'ref': 'origin/main',
            }
            with mock.patch.object(wt, 'materialize_peer', wraps=wt.materialize_peer) as spy_mat:
                with self.assertRaisesRegex(wt.WorktreeError, 'WT_LINK_PROTECTED_MAIN_REFUSED'):
                    wt.new('rig-review', 'jinni', f'202a{idx}', slug='x', workspace=self.ws, root=self.root,
                           links_config=escape_cfg)
                self.assertEqual(spy_mat.call_count, 0)
            assert_rolled_back('rig-review', 'jinni', f'202a{idx}', f'jinni/202a{idx}')

        with self.assertRaisesRegex(wt.WorktreeError, 'WT_LINK_PROTECTED_MAIN_REFUSED'):
            wt.new('rig-review', 'jinni', '202', slug='x', workspace=self.ws, root=self.root,
                   peers=[f'rig-play={repos["rig-play"]}'])
        assert_rolled_back('rig-review', 'jinni', '202', 'jinni/202')

        with mock.patch.dict(os.environ, {'STUB_BUN_MODE': 'escape_main', 'STUB_MAIN_ESCAPE': str(repos['rig-play'])}):
            with self.assertRaisesRegex(wt.WorktreeError, 'WT_LINK_PROTECTED_MAIN_REFUSED'):
                wt.new('rig-review', 'jinni', '203', slug='x', workspace=self.ws, root=self.root)
        assert_rolled_back('rig-review', 'jinni', '203', 'jinni/203')

        # 3. WT_LINK_DECLARATION_MISMATCH (version / internal dependency mismatch & post-install binding checks)
        bad_cfg = json.loads(json.dumps(links_cfg))
        bad_cfg['members']['rig-parts']['version'] = '9.9.9'
        with self.assertRaisesRegex(wt.WorktreeError, 'WT_LINK_DECLARATION_MISMATCH'):
            wt.new('rig-review', 'jinni', '204', slug='x', workspace=self.ws, root=self.root, links_config=bad_cfg)
        assert_rolled_back('rig-review', 'jinni', '204', 'jinni/204')

        for idx, bad_mode in enumerate(('registry_fallback', 'copy_fallback', 'wrong_peer'), start=1):
            with mock.patch.dict(os.environ, {'STUB_BUN_MODE': bad_mode}):
                with self.assertRaisesRegex(wt.WorktreeError, 'WT_LINK_DECLARATION_MISMATCH'):
                    wt.new('rig-review', 'jinni', f'204b{idx}', slug='x', workspace=self.ws, root=self.root)
            assert_rolled_back('rig-review', 'jinni', f'204b{idx}', f'jinni/204b{idx}')

        # 4. WT_LINK_SOURCE_CONDITION_MISSING (missing @synth/source export condition on rig-play)
        run(repos['rig-play'], 'checkout', '-qb', 'broken-exports')
        broken_pkg = json.loads((repos['rig-play'] / 'package.json').read_text(encoding='utf-8'))
        broken_pkg['exports'] = {'.': {'import': './dist/index.js'}}
        (repos['rig-play'] / 'package.json').write_text(json.dumps(broken_pkg, indent=2) + '\n', encoding='utf-8')
        run(repos['rig-play'], 'add', 'package.json')
        run(repos['rig-play'], 'commit', '-qm', 'drop source export')
        run(repos['rig-play'], 'checkout', '-q', 'main')
        main_snapshots['rig-play'] = self._snapshot_main_state(repos['rig-play'])
        with self.assertRaisesRegex(wt.WorktreeError, 'WT_LINK_SOURCE_CONDITION_MISSING'):
            wt.new('rig-review', 'jinni', '205', slug='x', workspace=self.ws, root=self.root,
                   peers=['rig-play=broken-exports'])
        assert_rolled_back('rig-review', 'jinni', '205', 'jinni/205')

        # 5. WT_LINK_INSTALL_FAILED (installer exits nonzero -> clean rollback without --force;
        #    installer dirties tracked primary lane -> removes .ws and derived node_modules, retains dirty worktree/branch)
        with mock.patch.dict(os.environ, {'STUB_BUN_MODE': 'fail'}):
            with self.assertRaisesRegex(wt.WorktreeError, 'WT_LINK_INSTALL_FAILED'):
                wt.new('rig-review', 'jinni', '206', slug='x', workspace=self.ws, root=self.root)
        assert_rolled_back('rig-review', 'jinni', '206', 'jinni/206')

        with mock.patch.dict(os.environ, {'STUB_BUN_MODE': 'dirty_lane'}):
            with self.assertRaisesRegex(wt.WorktreeError, 'WT_LINK_INSTALL_FAILED.*WT_LINK_ROLLBACK_REFUSED_DIRTY'):
                wt.new('rig-review', 'jinni', '207', slug='x', workspace=self.ws, root=self.root)
        dirty_lane = self.root / 'rig-review' / 'jinni-207'
        dirty_ws = wt.sidecar_path(dirty_lane)
        self.assertFalse(dirty_ws.exists())
        self.assertFalse((dirty_lane / 'node_modules').exists())
        self.assertTrue(dirty_lane.is_dir())
        self.assertGreater(wt.dirty_count(dirty_lane), 0)
        self.assertTrue(wt.read_owner(dirty_lane)['rollback_retained_dirty'])
        self.assertIn('jinni/207-x', run(repos['rig-review'], 'branch', '--list', 'jinni/207*'))
        run(dirty_lane, 'checkout', '--', 'package.json')
        wt.remove_owned(dirty_lane, workspace=self.ws)
        assert_rolled_back('rig-review', 'jinni', '207', 'jinni/207')

        for name, checkout in repos.items():
            self.assertEqual(self._snapshot_main_state(checkout), main_snapshots[name])

    def test_sidecar_cleanup_via_wt_remove_and_sweep_including_orphan_sidecars(self):
        repos, _, _, _ = self._setup_linked_workspace()
        # 1. Explicit owner cleanup via `wt remove` removes both worktree and owned sidecar without --force
        c1 = wt.new('rig-review', 'jinni', '301', slug='clean', session='sess-301', provider='agy',
                    workspace=self.ws, root=self.root)
        lane1, ws1 = Path(c1['path']), Path(c1['sidecar'])
        # Refuses removal by a different session
        with self.assertRaisesRegex(wt.WorktreeError, 'owned by session sess-301'):
            wt.remove_owned(lane1, session='other-session', workspace=self.ws)
        # Refuses removal when dirty (no --force)
        (lane1 / 'untracked.txt').write_text('dirty\n', encoding='utf-8')
        with self.assertRaisesRegex(wt.WorktreeError, 'dirty'):
            wt.remove_owned(lane1, session='sess-301', workspace=self.ws)
        (lane1 / 'untracked.txt').unlink()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bridge.main(['wt', 'remove', str(lane1), '--session', 'sess-301', '--workspace', str(self.ws)])
        self.assertEqual(rc, 0)
        removed1 = json.loads(buf.getvalue())
        self.assertTrue(removed1['removed'])
        self.assertTrue(removed1['sidecar_removed'])
        self.assertFalse(lane1.exists())
        self.assertFalse(ws1.exists())
        self.assertEqual(run(repos['rig-review'], 'branch', '--list', 'jinni/301*'), '')

        # 2. Absent-worktree (orphan) sidecar after raw `git worktree remove`
        c2 = wt.new('rig-review', 'jinni', '302', slug='orphan', session='sess-302', provider='agy',
                    workspace=self.ws, root=self.root)
        lane2, ws2 = Path(c2['path']), Path(c2['sidecar'])
        self.commit(lane2, 'feature.txt', 'orphan-test\n')
        # Simulate caller running raw `git worktree remove` after removing gitignored node_modules
        wt.rmtree_sidecar(lane2 / 'node_modules')
        run(repos['rig-review'], 'worktree', 'remove', str(lane2))
        self.assertFalse(lane2.exists())
        self.assertTrue(ws2.exists())

        # Also create an arbitrary unowned .ws directory to ensure it is NEVER deleted
        arbitrary_ws = self.root / 'rig-review' / 'arbitrary-unowned.ws'
        arbitrary_ws.mkdir(parents=True, exist_ok=True)
        (arbitrary_ws / 'keep.txt').write_text('do not delete\n', encoding='utf-8')

        # While unlanded on origin/main, sweep keeps the orphan sidecar
        rep_unlanded = self.sweep(apply=True, repo_filter='rig-review')
        orphan_row = self.row(rep_unlanded, ws2)
        self.assertEqual(orphan_row['action'], 'keep')
        self.assertIn('not landed: changed paths differ from origin/main', orphan_row['reasons'])
        self.assertTrue(ws2.exists())
        arb_row = self.row(rep_unlanded, arbitrary_ws)
        self.assertEqual(arb_row['action'], 'keep')
        self.assertTrue(arbitrary_ws.exists())

        # Land the change on rig-review origin/main; while owner session is alive, sweep still keeps it
        run(repos['rig-review'], 'checkout', '-q', 'main')
        (repos['rig-review'] / 'feature.txt').write_text('orphan-test\n', encoding='utf-8')
        run(repos['rig-review'], 'add', 'feature.txt')
        run(repos['rig-review'], 'commit', '-qm', 'land feature')
        run(repos['rig-review'], 'push', '-q', 'origin', 'main')
        run(repos['rig-review'], 'fetch', '-q', 'origin')

        live_owner = [{'provider': 'agy', 'id': 'sess-302', 'cwd': '/elsewhere', 'state': 'alive'}]
        rep_alive = self.sweep(apply=True, repo_filter='rig-review', live=live_owner)
        self.assertEqual(self.row(rep_alive, ws2)['action'], 'keep')
        self.assertTrue(ws2.exists())

        # Once owner session is gone and change is landed, sweep --apply removes the orphan sidecar & branch,
        # while keeping the arbitrary unowned .ws directory untouched
        rep_dead = self.sweep(apply=True, repo_filter='rig-review', live=())
        self.assertEqual(self.row(rep_dead, ws2)['action'], 'remove')
        self.assertTrue(self.row(rep_dead, ws2)['result']['removed'])
        self.assertFalse(ws2.exists())
        self.assertEqual(run(repos['rig-review'], 'branch', '--list', 'jinni/302*'), '')
        self.assertTrue((arbitrary_ws / 'keep.txt').is_file())

        # 3. Linked worktree + sidecar together removed by sweep --apply when clean, landed, and owner dead
        c3 = wt.new('rig-review', 'jinni', '303', slug='both', session='sess-303', provider='agy',
                    workspace=self.ws, root=self.root)
        lane3, ws3 = Path(c3['path']), Path(c3['sidecar'])
        with mock.patch.object(wt, 'open_files', return_value=[]):
            rep_both = self.sweep(apply=True, repo_filter='rig-review', live=())
        self.assertEqual(self.row(rep_both, lane3)['action'], 'remove')
        self.assertTrue(self.row(rep_both, lane3)['result']['removed'])
        self.assertTrue(self.row(rep_both, lane3)['result']['sidecar_removed'])
        self.assertFalse(lane3.exists())
        self.assertFalse(ws3.exists())
        self.assertEqual(run(repos['rig-review'], 'branch', '--list', 'jinni/303*'), '')


if __name__ == '__main__':
    unittest.main()
