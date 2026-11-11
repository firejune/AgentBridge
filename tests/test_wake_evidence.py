"""Issue #121: keep a normalized wake outcome with the message; nothing retries a push."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
import message_store
import peer_wake
import sessions
from support import RunningBroker
from test_peer import FakeClaude
from test_wake import WakeFixture, HOST, DESKTOP

STDERR = 'LSOpenURLsWithRole() failed with error -54 for /private/secret/path'


def refuse(url, timeout=None):
    raise sessions.DiscoveryError('The link claude:// was not opened: ' + STDERR)


class WakeEvidenceTests(WakeFixture):
    def env(self):
        return mock.patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':self.temp.name,
                                           'CLAUDE_DESKTOP_SESSIONS_DIR':str(self.root/'desktop')})

    def send(self, server, key='k1'):
        server.call('register',project='demo',name='alpha',cwd=self.temp.name)
        server.call('register',project='demo',name='beta',cwd=self.temp.name,transport='claude-peer',session=self.sid)
        return server.call('send',project='demo',sender='alpha',recipient='beta',body='Question',key=key)

    def kept(self, server):
        return (Path(server.state_dir)/'correspondence.json').read_text()

    def test_woken_push_keeps_the_wake_after_answer_and_restart(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()), \
                mock.patch.object(bridge,'TITLE_WAIT',0.0):
            message = self.send(server)
            self.assertEqual(bridge.push(server.path,message,wake_timeout=2)['state'],'submitted')
            server.call('reply',id=message['id'],project='demo',body='Answer',key='r1')
            server.restart()
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'answered')
            self.assertEqual(stored['events'],[])  # The checkpoint never keeps events: why #121 lost the outcome.
            wake, = stored['wakes']
            self.assertEqual(set(wake),set(peer_wake.FIELDS))
            self.assertEqual((wake['state'],wake['code'],wake['source'],wake['attempt']),('woken','woken','push',1))
            self.assertIsInstance(wake['elapsed'],float)
            self.assertIsNotNone(wake['at'])
            text = self.kept(server)
            for absent in (HOST,'claude://',str(self.fakes[0].sock),'messagingSocketPath','restarted in Claude Desktop'):
                self.assertNotIn(absent,text)

    def test_refused_link_keeps_only_its_code(self):
        # Before #121 the opener's refusal escaped wake_claude: the result had no wake at all.
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',refuse), \
                mock.patch.object(bridge,'push_frame') as frame:
            message = self.send(server)
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path,message,wake_timeout=2)
            self.assertEqual(caught.exception.message['wake']['state'],'failed')
            self.assertIn('-54',server.call('show',id=message['id'])['note'])  # Volatile only.
            server.restart()
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual((stored['wakes'][-1]['state'],stored['wakes'][-1]['code']),('failed','opener_failed'))
            self.assertNotIn('peer_retry',stored)
            self.assertNotIn('-54',self.kept(server))
            self.assertNotIn('secret',self.kept(server))
            frame.assert_not_called()

    def test_nothing_pushes_or_wakes_a_pending_message_on_its_own(self):
        self.metadata()
        opener = mock.Mock(side_effect=refuse)
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',opener), \
                mock.patch.object(bridge,'push_frame') as frame:
            message = self.send(server)
            with self.assertRaises(bridge.PushFailed):
                bridge.push(server.path,message,wake_timeout=2)
            time.sleep(1.5)
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual(len(stored['wakes']),1)
            self.assertEqual(opener.call_count,1)
            frame.assert_not_called()

    def test_each_wake_is_one_numbered_entry(self):
        with RunningBroker() as server, self.env(), mock.patch.object(bridge,'push_frame'):
            message = self.send(server)
            for _ in range(2):  # No Desktop session maps to it: unsupported, nothing opened.
                with self.assertRaises(bridge.PushFailed):
                    bridge.push(server.path,message,wake_timeout=2)
            stored = server.call('show',id=message['id'])
            self.assertEqual([(w['code'],w['attempt']) for w in stored['wakes']],
                             [('no_desktop_session',1),('no_desktop_session',2)])
            self.assertEqual(self.urls,[])

    def test_uncertain_and_budget_wakes_are_coded(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener(socket=False)), \
                mock.patch.object(bridge,'push_frame'):
            message = self.send(server)
            with self.assertRaises(bridge.PushFailed):
                bridge.push(server.path,message,wake_timeout=0.2)
            with self.assertRaises(bridge.PushFailed):
                bridge.push(server.path,message,deadline=time.monotonic()+bridge.PEER_IO_TIMEOUT-1)
            stored = server.call('show',id=message['id'])
            self.assertEqual([(w['state'],w['code']) for w in stored['wakes']],
                             [('uncertain','inbox_timeout'),('unsupported','budget_spent')])

    def test_live_push_records_no_wake(self):
        self.fakes.append(FakeClaude(self.temp.name,self.sid,extra=DESKTOP))
        with RunningBroker() as server, self.env(), mock.patch.object(bridge,'TITLE_WAIT',0.0):
            message = self.send(server)
            bridge.push(server.path,message)
            self.assertNotIn('wakes',server.call('show',id=message['id']))


class CheckpointTests(unittest.TestCase):
    def message(self, **extra):
        route = {'project':'demo','name':'beta','transport':'claude-peer','cwd':'/tmp',
                 'session':'00000000-0000-4000-8000-000000000000','executable':None}
        origin = dict(route, name='alpha', transport='realtime', session=None)
        return {'id':'11111111-1111-4111-8111-111111111111','conversation':'22222222-2222-4222-8222-222222222222',
                'project':'demo','sender':'alpha','recipient':'beta','channel':None,'sender_channel':None,
                'body':'Question','reply_to':None,'state':'pending','created':'2026-11-11T00:00:00+00:00',
                'route':route,'return_route':origin,'events':[],'note':'secret note',**extra}

    GOOD = {'state':'failed','code':'opener_failed','elapsed':0.1,'at':'2026-11-11T00:00:00+00:00',
            'source':'push','attempt':2}

    def test_malformed_and_unhashable_entries_are_dropped_one_by_one(self):
        bad = [{'state':['woken'],'code':'woken','elapsed':None,'at':None,'source':'push','attempt':1},
               {**self.GOOD,'code':{'x':1}}, {**self.GOOD,'source':['push']}, {**self.GOOD,'attempt':True},
               {**self.GOOD,'elapsed':float('nan')}, {**self.GOOD,'at':['t']}, {**self.GOOD,'url':'claude://x'},
               {'state':'woken'}, ['woken'], None, 7]
        record = message_store.message_record(self.message(
            wakes=bad+[self.GOOD], peer_retry={'status':['waiting'],'attempts':{}}))
        self.assertEqual(record['wakes'],[self.GOOD])
        self.assertNotIn('peer_retry',record)
        self.assertIsNone(record['note'])
        for wakes in ({'state':'woken'}, 'woken', 3, [[1,2]], [{1,2}]):
            self.assertNotIn('wakes',message_store.message_record(self.message(wakes=wakes)))

    def test_a_checkpoint_with_bad_wake_fields_still_loads(self):
        with tempfile.TemporaryDirectory(prefix='abw-',dir='/tmp') as temp:
            directory = Path(temp)
            os.chmod(directory,0o700)
            raw = self.message(wakes=[{'state':['x']}, {**self.GOOD,'code':[1]}, self.GOOD],
                               peer_retry={'status':{}})
            path = directory/'correspondence.json'
            path.write_text(json.dumps({'version':1,'messages':[raw],'keys':[],'receipts':[],'snapshots':{}}))
            os.chmod(path,0o600)
            loaded = message_store.MessageStore(directory,16).load()
            self.assertFalse((directory/'correspondence-lost.json').exists())
            message, = loaded['messages']
            self.assertEqual(message['wakes'],[self.GOOD])


if __name__ == '__main__':
    unittest.main()
