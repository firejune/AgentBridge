"""Issue #121: keep the wake outcome on the message, and retry pushes left pending by a closed inbox."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
import broker
import message_store
import peer_retry
import provider_failures
import sessions
from support import RunningBroker
from test_wake import WakeFixture, HOST


def refuse(url, timeout=None):
    raise sessions.DiscoveryError('The link claude:// was not opened: LSOpenURLsWithRole() failed with error -54')


class Senders(WakeFixture):
    def env(self):
        return mock.patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':self.temp.name,
                                           'CLAUDE_DESKTOP_SESSIONS_DIR':str(self.root/'desktop')})

    def register(self, call):
        call('register',project='demo',name='alpha',cwd=self.temp.name)
        call('register',project='demo',name='beta',cwd=self.temp.name,transport='claude-peer',session=self.sid)

    def send(self, call, key='k1'):
        return call('send',project='demo',sender='alpha',recipient='beta',body='Question',key=key)


class WakeEvidenceTests(Senders):
    """The wake outcome survives the answer, completion and a broker restart."""
    def test_woken_push_keeps_the_wake_after_answer_and_restart(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()), \
                mock.patch.object(bridge,'TITLE_WAIT',0.0):
            self.register(server.call)
            message = self.send(server.call)
            self.assertEqual(bridge.push(server.path,message,wake_timeout=2)['state'],'submitted')
            server.call('reply',id=message['id'],project='demo',body='Answer',key='r1')
            server.restart()
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'answered')
            self.assertEqual(stored['events'],[])  # The checkpoint never keeps events: why #121 lost the outcome.
            wake, = stored['wakes']
            self.assertEqual((wake['state'],wake['source'],wake['attempt']),('woken','send',0))
            self.assertEqual(wake['url'],f'claude://claude.ai/epitaxy/{HOST}')
            self.assertEqual(wake['desktop_session'],HOST)
            self.assertIsInstance(wake['elapsed'],float)
            self.assertIsNotNone(wake['at'])
            self.assertNotIn('peer_retry',stored)
            text = (Path(server.state_dir)/'correspondence.json').read_text()
            self.assertNotIn('messagingSocketPath',text)
            self.assertNotIn(str(self.fakes[0].sock),text)  # No endpoint is kept.

    def test_refused_link_is_a_failed_wake_and_schedules_a_retry(self):
        # Before #121 the opener's refusal escaped wake_claude: the result had no wake at all.
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',refuse), \
                mock.patch.object(bridge,'push_frame') as frame:
            self.register(server.call)
            message = self.send(server.call)
            before = time.time()
            with self.assertRaises(bridge.PushFailed) as caught:
                bridge.push(server.path,message,wake_timeout=2)
            self.assertEqual(caught.exception.message['wake']['state'],'failed')
            server.restart()
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual(stored['wakes'][-1]['state'],'failed')
            self.assertIn('-54',stored['wakes'][-1]['reason'])
            retry = stored['peer_retry']
            self.assertEqual((retry['status'],retry['attempts'],retry['failure']),('waiting',0,'closed'))
            self.assertGreaterEqual(retry['next_at'],before+peer_retry.DEFAULT_BACKOFF[0])
            frame.assert_not_called()

    def test_uncertain_wake_is_kept_and_never_retried(self):
        self.metadata()
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener(socket=False)), \
                mock.patch.object(bridge,'push_frame') as frame:
            self.register(server.call)
            message = self.send(server.call)
            with self.assertRaises(bridge.PushFailed):
                bridge.push(server.path,message,wake_timeout=0.2)
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual(stored['wakes'][-1]['state'],'uncertain')
            self.assertEqual((stored['peer_retry']['status'],stored['peer_retry']['failure']),('stopped','wake-uncertain'))
            self.assertIsNone(stored['peer_retry']['next_at'])
            frame.assert_not_called()

    def test_unsupported_wake_is_a_closed_inbox(self):
        # No Desktop session maps to it: nothing is opened, but the person may reopen it later.
        with RunningBroker() as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()):
            self.register(server.call)
            message = self.send(server.call)
            with self.assertRaises(bridge.PushFailed):
                bridge.push(server.path,message,wake_timeout=2)
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['wakes'][-1]['state'],'unsupported')
            self.assertEqual(stored['peer_retry']['status'],'waiting')
            self.assertEqual(self.urls,[])

    def test_live_push_records_no_wake_and_no_schedule(self):
        from test_peer import FakeClaude
        from test_wake import DESKTOP
        self.fakes.append(FakeClaude(self.temp.name,self.sid,extra=DESKTOP))
        with RunningBroker() as server, self.env(), mock.patch.object(bridge,'TITLE_WAIT',0.0):
            self.register(server.call)
            message = self.send(server.call)
            bridge.push(server.path,message)
            stored = server.call('show',id=message['id'])
            self.assertNotIn('wakes',stored)
            self.assertNotIn('peer_retry',stored)


class CheckpointTests(unittest.TestCase):
    def message(self, **extra):
        route = {'project':'demo','name':'beta','transport':'claude-peer','cwd':'/tmp','session':'0'*8+'-0000-4000-8000-'+'0'*12,
                 'executable':None}
        origin = dict(route, name='alpha', transport='realtime', session=None)
        return {'id':'11111111-1111-4111-8111-111111111111','conversation':'22222222-2222-4222-8222-222222222222',
                'project':'demo','sender':'alpha','recipient':'beta','channel':None,'sender_channel':None,
                'body':'Question','reply_to':None,'state':'pending','created':'2026-11-11T00:00:00+00:00',
                'route':route,'return_route':origin,'events':[],'note':'secret note',**extra}

    def test_malformed_wake_and_schedule_are_dropped_not_the_checkpoint(self):
        good = {'state':'failed','reason':'x','url':None,'desktop_session':None,'host':None,'elapsed':0.1,
                'at':'2026-11-11T00:00:00+00:00','source':'retry','attempt':2}
        record = message_store.message_record(self.message(
            wakes=[{'state':'woken','peer':{'socket':'/tmp/s'}}, good, {'state':'nope'}],
            peer_retry={'status':'waiting','attempts':99,'cap':1,'next_at':1.0,'failure':'closed','reason':None}))
        self.assertEqual(record['wakes'],[good])
        self.assertNotIn('peer_retry',record)
        self.assertIsNone(record['note'])

    def test_a_retry_cut_by_a_restart_waits_again(self):
        message = {'peer_retry':{'status':'retrying','attempts':1,'cap':3,'next_at':None,'failure':'closed','reason':None}}
        peer_retry.restored(message)
        self.assertEqual(message['peer_retry']['status'],'waiting')
        self.assertLessEqual(message['peer_retry']['next_at'],time.time())


class InProcessBroker:
    """A broker State behind a Unix socket on a private event loop, so retries run real pushes in-process."""
    def __init__(self, root, backoff=(1,), cap=3, interval=0.05):
        self.path = str(Path(root)/'r.sock')
        self.state = broker.State()
        self.state.peer_retries.policy = provider_failures.retry_policy(backoff, cap)
        self.interval = interval
        self.pushes = []

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def pusher(path, m):
            self.pushes.append(m['id'])
            return bridge.retry_peer(path, m)

        async def handle(reader, writer):
            try:
                args = json.loads(await reader.readline())
                try:
                    reply = {'result':self.state.action(args),'instance':self.state.instance}
                except (ValueError,KeyError,TypeError) as error:
                    reply = {'error':str(error),'instance':self.state.instance}
                writer.write(broker.wire(reply))
                await writer.drain()
            finally:
                writer.close()

        async def main():
            self.server = await asyncio.start_unix_server(handle, path=self.path)
            self.observer = asyncio.ensure_future(peer_retry.observe(self.state, self.path, pusher, self.interval))
            ready.set()
            await self.stopped

        def run():
            asyncio.set_event_loop(self.loop)
            self.stopped = self.loop.create_future()
            self.loop.run_until_complete(main())
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(5)
        return self

    def call(self, op, **args):
        return broker.request(self.path, {'op':op, **args})

    def __exit__(self, *_):
        def stop():
            self.observer.cancel()
            self.server.close()
            self.stopped.set_result(None)
        self.loop.call_soon_threadsafe(stop)
        self.thread.join(10)


class RetryTests(Senders):
    def until(self, test, limit=8):
        stop = time.monotonic()+limit
        while time.monotonic() < stop:
            if test():
                return True
            time.sleep(0.05)
        return False

    def first_closed_push(self, server):
        self.register(server.call)
        message = self.send(server.call)
        with self.assertRaises(bridge.PushFailed):
            bridge.push(server.path,message,wake_timeout=2)
        return message

    def test_retry_wakes_once_and_delivers_once(self):
        self.metadata()
        opener = mock.Mock(side_effect=refuse)  # The sender's link is refused (e.g. outside its sandbox).
        with InProcessBroker(self.temp.name) as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',opener), \
                mock.patch.object(bridge,'TITLE_WAIT',0.0):
            message = self.first_closed_push(server)
            opener.side_effect = self.opener()  # The broker's own try opens it.
            self.assertTrue(self.until(lambda: server.call('show',id=message['id'])['state']=='submitted'))
            time.sleep(0.3)  # No second push after success.
            stored = server.call('show',id=message['id'])
            self.assertEqual(server.pushes,[message['id']])
            self.assertEqual(opener.call_count,2)  # One wake per push: the sender's and the retry's.
            self.assertEqual(len(self.fakes),1)
            self.assertEqual(len(self.fakes[0].received),1)
            self.assertEqual([(w['state'],w['source'],w['attempt']) for w in stored['wakes']],
                             [('failed','send',0),('woken','retry',1)])
            self.assertEqual((stored['peer_retry']['status'],stored['peer_retry']['attempts']),('delivered',1))

    def test_retries_stop_at_the_cap(self):
        # Nothing maps to a Desktop session: every try is a closed inbox and opens nothing.
        with InProcessBroker(self.temp.name,backoff=(1,),cap=2) as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()):
            message = self.first_closed_push(server)
            self.assertTrue(self.until(lambda: server.call('show',id=message['id'])['peer_retry']['status']=='exhausted'))
            time.sleep(1.3)
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual(server.pushes,[message['id']]*2)
            self.assertEqual(stored['peer_retry']['attempts'],2)
            self.assertIsNone(stored['peer_retry']['next_at'])
            self.assertEqual([w['source'] for w in stored['wakes']],['send','retry','retry'])
            self.assertEqual(self.urls,[])

    def test_an_uncertain_retry_wake_stops_retries(self):
        self.metadata()
        opener = mock.Mock(side_effect=refuse)
        with InProcessBroker(self.temp.name) as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',opener), \
                mock.patch.object(bridge,'push_frame') as frame:
            message = self.first_closed_push(server)
            opener.side_effect = self.opener(socket=False)  # The link reaches the app; no inbox appears.
            with mock.patch.object(bridge,'retry_peer',lambda path,m: bridge.push(path,m,wake_timeout=0.3)):
                self.assertTrue(self.until(lambda: server.call('show',id=message['id'])['peer_retry']['status']=='stopped'))
            time.sleep(1.3)
            stored = server.call('show',id=message['id'])
            self.assertEqual(stored['state'],'pending')
            self.assertEqual(server.pushes,[message['id']])
            self.assertEqual(opener.call_count,2)
            self.assertEqual(stored['wakes'][-1]['state'],'uncertain')
            frame.assert_not_called()

    def test_claimed_or_uncertain_messages_are_never_retried(self):
        with InProcessBroker(self.temp.name) as server, self.env(), \
                mock.patch.object(sessions,'open_url_background',self.opener()):
            message = self.first_closed_push(server)
            server.call('claim',id=message['id'])  # Another deliverer holds it when it comes due.
            time.sleep(1.4)
            self.assertEqual(server.pushes,[])
            server.call('finish',id=message['id'],state='uncertain',note='bytes may have reached the receiver')
            server.call('resolve',id=message['id'],state='pending',note='operator returned it')
            time.sleep(1.4)
            self.assertEqual(server.pushes,[])
            self.assertEqual(server.call('show',id=message['id'])['peer_retry']['status'],'stopped')


if __name__ == '__main__':
    unittest.main()
