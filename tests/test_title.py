import json
import os
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
from test_peer import FakeClaude


def titles(path):
    return [json.loads(line)['customTitle'] for line in path.read_text().splitlines()
            if '"custom-title"' in line]


class ClaudeTitleTests(unittest.TestCase):
    """Issue #85: the route name becomes the session's resume-picker label."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='abt-',dir='/tmp')
        self.root = Path(self.temp.name)
        self.sid = str(uuid.uuid4())
        self.projects = self.root/'projects'
        self.transcript = self.projects/'-tmp-work'/f'{self.sid}.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text(json.dumps({'type':'user','sessionId':self.sid})+'\n')
        self.env = {**os.environ,'CLAUDE_CONFIG_DIR':self.temp.name}

    def tearDown(self):
        self.temp.cleanup()

    def title(self, text, **kw):
        return sessions.claude_title(self.sid, text, projects_dir=self.projects, **kw)

    def test_writes_once_then_keeps(self):
        self.assertEqual(self.title('rig-c/parts-tower')['state'],'written')
        self.assertEqual(self.title('rig-c/parts-tower')['state'],'kept')
        self.assertEqual(titles(self.transcript),['rig-c/parts-tower'])
        self.assertIn('{"type":"custom-title","customTitle":"rig-c/parts-tower",', self.transcript.read_text())
        self.assertEqual(self.title('rig-c/parts-tower',force=True)['state'],'written')

    def test_never_overwrites_a_user_title_unless_forced(self):
        with self.transcript.open('a') as handle:
            handle.write(json.dumps({'type':'custom-title','customTitle':'mine','sessionId':self.sid})+'\n')
        kept = self.title('rig-c/parts-tower')
        self.assertEqual((kept['state'],kept['title']),('kept','mine'))
        forced = self.title('rig-c/parts-tower',force=True)
        self.assertEqual((forced['state'],forced['previous']),('written','mine'))
        self.assertEqual(titles(self.transcript),['mine','rig-c/parts-tower'])

    def test_ai_title_does_not_block(self):
        with self.transcript.open('a') as handle:
            handle.write(json.dumps({'type':'ai-title','aiTitle':'auto','sessionId':self.sid})+'\n')
        self.assertEqual(self.title('p/n')['state'],'written')

    def test_skips_without_exactly_one_transcript(self):
        other = str(uuid.uuid4())
        self.assertIn('no transcript',sessions.claude_title(other,'p/n',projects_dir=self.projects)['reason'])
        twin = self.projects/'-tmp-other'/f'{self.sid}.jsonl'
        twin.parent.mkdir()
        twin.write_text('')
        self.assertIn('2 transcripts',self.title('p/n')['reason'])

    def test_skips_a_tail_that_is_mid_write(self):
        with self.transcript.open('a') as handle:
            handle.write('{"type":"assi')
        self.assertEqual(self.title('p/n')['state'],'skipped')
        self.assertEqual(titles(self.transcript),[])

    def cli(self, server, *args):
        return subprocess.run([sys.executable,bridge.__file__,'--socket',server.path,*args],
                              input='hi',capture_output=True,text=True,env=self.env)

    def test_register_labels_and_title_command_works_on_dead_session(self):
        fake = FakeClaude(self.temp.name,self.sid)
        try:
            with RunningBroker() as server:
                out = self.cli(server,'register','tower','--project','demo','--transport','claude-peer',
                               '--session',self.sid,'--discover')
                self.assertEqual(out.returncode,0,out.stdout)
                self.assertEqual(json.loads(out.stdout)['title']['state'],'written')
                fake.close()
                (fake.registry/f'{fake.pid}.json').unlink()  # the process is gone; the route stays
                again = self.cli(server,'title','tower','--project','demo')
                self.assertEqual(again.returncode,0,again.stdout)
                self.assertEqual(json.loads(again.stdout)['state'],'kept')
                missing = self.cli(server,'title','nobody','--project','demo')
                self.assertEqual(missing.returncode,1)
        finally:
            fake.close()
        self.assertEqual(titles(self.transcript),['demo/tower'])


if __name__=='__main__':
    unittest.main()
