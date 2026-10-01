import concurrent.futures
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'mail.db'
        self.db = bridge.connect(self.path)
        for name in ('naru', 'roa'):
            bridge.register(self.db,'demo',name,'inbox',None,str(self.root),None)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def send(self, key='request-1'):
        return bridge.post(self.db,'demo','roa','naru','Review the view',key)

    def fake(self, transport='codex', output=None, exit_code=0):
        exe = self.root / 'fake-cli'
        # Argument capture proves the shell never interprets message text.
        exe.write_text('#!' + sys.executable + '\nimport sys,json\n'
                       'from pathlib import Path\n'
                       f'Path({str(self.root / "argv.json")!r}).write_text(json.dumps(sys.argv))\n'
                       f'print({(output or "queued")!r})\nraise SystemExit({exit_code})\n')
        exe.chmod(0o700)
        bridge.register(self.db,'demo','naru',transport,str(uuid.uuid4()),str(self.root),str(exe))
        return exe

    def test_idempotence_and_conflicting_key(self):
        first = self.send()
        self.assertEqual(first['id'], self.send()['id'])
        with self.assertRaisesRegex(ValueError,'different content'):
            bridge.post(self.db,'demo','roa','naru','Changed','request-1')
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)

    def test_reply_direction_and_atomic_parent_completion(self):
        first = self.send()
        with self.assertRaisesRegex(ValueError,'reverse'):
            bridge.post(self.db,'demo','roa','naru','Wrong','wrong',first['id'])
        answer = bridge.post(self.db,'demo','naru','roa','Ready','answer',first['id'])
        self.assertEqual(bridge.message(self.db,first['id'])['state'],'answered')
        self.assertEqual(answer['reply_to'],first['id'])

    def test_frozen_route_and_shell_literals(self):
        self.fake()
        body = "quotes ' \" ; $(touch SHOULD_NOT_EXIST) `touch ALSO_NOT`\nnext line"
        m = bridge.post(self.db,'demo','roa','naru',body,'literal')
        session = json.loads(m['route'])['session']
        bridge.register(self.db,'demo','naru','inbox',None,str(self.root),None)
        result = bridge.deliver(self.db,m['id'])
        self.assertEqual(result['state'],'submitted')
        args = json.loads((self.root / 'argv.json').read_text())
        self.assertIn(session,args)
        self.assertTrue(args[-1].endswith(body))
        self.assertFalse((self.root / 'SHOULD_NOT_EXIST').exists())
        self.assertFalse((self.root / 'ALSO_NOT').exists())
        with self.assertRaisesRegex(ValueError,'Only pending'):
            bridge.deliver(self.db,m['id'])

    def test_dry_run_has_no_side_effect(self):
        self.fake()
        m = self.send()
        self.assertFalse(bridge.deliver(self.db,m['id'],dry_run=True)['sent'])
        self.assertFalse((self.root/'argv.json').exists())
        self.assertEqual(bridge.message(self.db,m['id'])['state'],'pending')

    def test_claude_response_returns_to_sender(self):
        self.fake('claude', json.dumps({'result':'View reviewed','is_error':False}))
        m = self.send()
        self.assertEqual(bridge.deliver(self.db,m['id'])['state'],'answered')
        reply = dict(self.db.execute('SELECT * FROM messages WHERE reply_to=?',(m['id'],)).fetchone())
        self.assertEqual((reply['sender'],reply['recipient'],reply['body']),('naru','roa','View reviewed'))
        self.assertEqual(reply['state'],'pending')  # no automatic ping-pong

    def test_invalid_claude_result_does_not_claim_completion(self):
        for output in ('not JSON', '[]',json.dumps({'result':'Done','is_error':True}),
                       json.dumps({'result':'Done','permission_denials':[{'tool':'Read'}]})):
            self.fake('claude',output)
            m = self.send(str(uuid.uuid4()))
            self.assertEqual(bridge.deliver(self.db,m['id'])['state'],'uncertain')

    def test_nonzero_exit_is_ambiguous_and_not_retried(self):
        self.fake(exit_code=2)
        m = self.send()
        self.assertEqual(bridge.deliver(self.db,m['id'])['state'],'uncertain')
        with self.assertRaises(ValueError):
            bridge.deliver(self.db,m['id'])

    def test_missing_executable_is_definite_failure(self):
        bridge.register(self.db,'demo','naru','codex',str(uuid.uuid4()),str(self.root),str(self.root/'absent'))
        m = self.send()
        self.assertEqual(bridge.deliver(self.db,m['id'])['state'],'failed')

    def test_timeout_is_uncertain(self):
        exe = self.fake()
        exe.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(10)\n')
        m = self.send()
        self.assertEqual(bridge.deliver(self.db,m['id'],timeout=0.05)['state'],'uncertain')

    def test_concurrent_identical_sends_have_one_id(self):
        def send(_):
            db = bridge.connect(self.path)
            try:
                return bridge.post(db,'demo','roa','naru','same','concurrent')['id']
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(send,range(8)))
        self.assertEqual(len(set(ids)),1)

    def test_dispatch_claim_is_exclusive(self):
        m = self.send()
        def claim(_):
            db = bridge.connect(self.path)
            try:
                return bridge.transition(db,m['id'],'pending','dispatching')
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(claim,range(8)))
        self.assertEqual(sum(results),1)

    def test_session_gate_blocks_distinct_messages_after_ambiguous_send(self):
        self.fake(exit_code=2)
        first = self.send('first')
        second = self.send('second')
        bridge.deliver(self.db,first['id'])
        with self.assertRaisesRegex(ValueError,'active or uncertain'):
            bridge.deliver(self.db,second['id'])
        self.assertEqual(bridge.message(self.db,second['id'])['state'],'pending')

    def setup_channels(self):
        for name, channel in (('naru','modal-view'), ('roa','game-logic')):
            bridge.register(self.db,'demo',name,'codex',str(uuid.uuid4()),str(self.root),None)
            bridge.bind_channel(self.db,'demo',channel,name,channel)

    def channel_send(self, key='channel-request'):
        return bridge.post(self.db,'demo',None,None,'Review state contract',key,
                           channel='modal-view',sender_channel='game-logic')

    def test_channel_handoff_preserves_both_addresses(self):
        self.setup_channels()
        first = self.channel_send()
        old_destination = json.loads(first['route'])['session']
        old_return = json.loads(first['return_route'])['session']
        for name, channel in (('naru','modal-view'), ('roa','game-logic')):
            bridge.register(self.db,'demo',name,'codex',str(uuid.uuid4()),str(self.root),None)
            bridge.bind_channel(self.db,'demo',channel,name,'new commander',expected=1)
        second = self.channel_send('new-request')
        self.assertNotEqual(json.loads(second['route'])['session'],old_destination)
        self.assertNotEqual(json.loads(second['return_route'])['session'],old_return)
        answer = bridge.reply(self.db,first['id'],'Contract accepted','channel-reply')
        self.assertEqual(json.loads(answer['route'])['session'],old_return)
        self.assertEqual(json.loads(answer['return_route'])['session'],old_destination)
        self.assertEqual((answer['channel'],answer['sender_channel']),('game-logic','modal-view'))
        self.assertEqual(json.loads(answer['route'])['generation'],1)
        self.assertEqual(self.channel_send()['id'],first['id'])  # retry cannot reroute

    def test_handoff_requires_current_generation(self):
        self.setup_channels()
        with self.assertRaisesRegex(ValueError,'generation changed'):
            bridge.bind_channel(self.db,'demo','modal-view','naru','overwrite without reading')
        self.assertEqual(bridge.channel_route(self.db,'demo','modal-view')[1]['generation'],1)

    def test_channel_lookup_is_project_scoped_and_closed_channel_refuses_new_mail(self):
        self.setup_channels()
        with self.assertRaisesRegex(ValueError,'missing or closed'):
            bridge.channel_route(self.db,'another-project','modal-view')
        original = self.channel_send()
        self.db.execute("UPDATE channels SET active=0 WHERE name='game-logic'")
        with self.assertRaisesRegex(ValueError,'missing or closed'):
            self.channel_send('closed-request')
        # An existing conversation can still respond to its original session.
        self.assertEqual(bridge.reply(self.db,original['id'],'Done','closed-reply')['channel'],'game-logic')

    def test_legacy_mailbox_migration_does_not_invent_return_address(self):
        legacy = self.root / 'legacy.db'
        db = sqlite3.connect(legacy)
        db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, project TEXT, sender TEXT, recipient TEXT, "
                   "body TEXT, reply_to TEXT, dedupe TEXT UNIQUE, route TEXT, state TEXT, note TEXT, created TEXT)")
        db.execute("INSERT INTO messages VALUES('old','demo','roa','naru','old request',NULL,'old-key','{}','pending',NULL,'today')")
        db.commit(); db.close()
        db = bridge.connect(legacy)
        try:
            self.assertIsNone(bridge.message(db,'old')['return_route'])
            with self.assertRaisesRegex(ValueError,'no frozen return address'):
                bridge.reply(db,'old','answer','legacy-answer')
        finally:
            db.close()

    def test_cli_channel_directory_and_reply(self):
        self.setup_channels()
        def run(*args, body=None):
            result = subprocess.run([sys.executable,bridge.__file__,'--db',str(self.path),*args],
                                    input=body,text=True,capture_output=True,check=True)
            return json.loads(result.stdout)
        channels = run('channels','--project','demo')
        self.assertEqual([c['name'] for c in channels],['game-logic','modal-view'])
        request = run('send','--project','demo','--from-channel','game-logic','--channel','modal-view',
                      '--key','cli-channel','--file','-',body='Question')
        inbox = run('inbox','--project','demo','--channel','modal-view')
        self.assertEqual(inbox[0]['generation'],1)
        self.assertEqual(inbox[0]['id'],request['id'])
        answer = run('reply',request['id'],'--key','cli-channel-reply','--file','-',body='Answer')
        self.assertEqual(answer['channel'],'game-logic')
        run('channel-close','modal-view','--project','demo','--expect-generation','1')
        closed = next(c for c in run('channels','--project','demo') if c['name']=='modal-view')
        self.assertEqual((closed['active'],closed['generation']),(0,2))

    def test_cli_roundtrip_inbox(self):
        cli = Path(bridge.__file__)
        def run(*args, body=None):
            result = subprocess.run([sys.executable,str(cli),'--db',str(self.path),*args],
                                    input=body,text=True,capture_output=True,check=True)
            return json.loads(result.stdout)
        m = run('send','--project','demo','--from','roa','--to','naru',
                '--file','-','--key','cli-request',body='Connect this state to the UI')
        self.assertEqual(run('inbox','naru','--project','demo')[0]['id'],m['id'])
        response = run('send','--project','demo','--from','naru','--to','roa',
                      '--file','-','--key','cli-answer','--reply-to',m['id'],body='Connected and verified')
        self.assertEqual(run('inbox','roa','--project','demo')[0]['id'],response['id'])
        self.assertEqual(run('show',m['id'])['state'],'answered')
        self.assertEqual(run('inbox','naru','--project','demo'),[])


if __name__ == '__main__':
    unittest.main()
