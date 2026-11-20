"""Issue #133 acceptance through a disposable Unix socket and state directory."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import broker
import message_store
import route_store


class CheckpointSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ab133-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'broker.sock'
        self.state_dir = self.root / 'state'
        self.checkpoint = self.state_dir / 'correspondence.json'
        self.clients = set()

    async def start(self):
        serving = asyncio.create_task(broker.serve(
            self.path, state_dir=self.state_dir, max_messages=3, usage_interval=0,
            codex_failure_interval=0, agy_failure_interval=0))
        self.addAsyncCleanup(self.stop, serving)
        deadline = time.monotonic() + 5
        while True:
            if serving.done():
                serving.result()
            try:
                await self.call('ping')
                return serving
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            self.assertLess(time.monotonic(), deadline, 'Broker did not create its socket')
            await asyncio.sleep(.01)

    async def stop(self, serving):
        if not serving.done():
            serving.cancel()
        await asyncio.wait_for(asyncio.gather(serving, return_exceptions=True), 10)

    @asynccontextmanager
    async def open_request(self, op, **fields):
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        try:
            writer.write(broker.wire(dict(op=op, **fields)))
            await writer.drain()
            yield reader
        finally:
            writer.close()
            await writer.wait_closed()

    async def result(self, reader):
        return broker.broker_response(await asyncio.wait_for(reader.readline(), 10))['result']

    async def call(self, op, **fields):
        async with self.open_request(op, **fields) as reader:
            return await self.result(reader)

    def queue(self, op, **fields):
        task = asyncio.create_task(self.call(op, **fields))
        self.clients.add(task)
        return task

    async def asyncTearDown(self):
        for task in self.clients:
            task.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)

    async def register(self):
        return {name: await self.call('register', project='demo', name=name,
                                      cwd=str(self.root), transport='realtime')
                for name in ('alpha', 'beta', 'gamma')}

    @staticmethod
    def send_fields(key, recipient='beta'):
        return dict(project='demo', sender='alpha', recipient=recipient, key=key, body=key)

    def saved_messages(self):
        return json.loads(self.checkpoint.read_bytes())['messages']

    @contextmanager
    def pause_next_write(self, *, delay=0, fail=False):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        write = message_store.atomic_write
        paused = False

        def pause(path, data, *, measure=None):
            nonlocal paused
            if path == self.checkpoint and not paused:
                paused = True
                loop.call_soon_threadsafe(started.set)
                time.sleep(delay)
                if not release.wait(8):
                    raise AssertionError('Test did not release the checkpoint worker')
                if fail:
                    raise OSError('fixture checkpoint write failed')
            write(path, data, measure=measure)

        with patch.object(message_store, 'atomic_write', side_effect=pause):
            try:
                yield started, release
            finally:
                release.set()

    async def test_ping_watch_order_and_timed_out_send_replay(self):
        serving = await self.start()
        await self.register()
        before = await self.call('ping')
        first = self.send_fields('first')
        second = self.send_fields('second', recipient='gamma')
        async with self.open_request('watch', project='demo', name='beta') as watcher:
            subscription = json.loads(await asyncio.wait_for(watcher.readline(), 5))
            self.assertEqual(subscription['type'], 'subscribed')
            with self.pause_next_write(delay=1.5) as (started, release):
                async with self.open_request('send', **first) as timed_out:
                    await asyncio.wait_for(started.wait(), 5)
                    now = time.monotonic()
                    during = await self.call('ping')
                    self.assertLess(time.monotonic() - now, .1)
                    self.assertEqual(during['checkpoint_bytes'], before['checkpoint_bytes'])
                    self.assertEqual(during['checkpoint_last_save_seconds'], before['checkpoint_last_save_seconds'])
                    self.assertEqual(during['messages'], 0)
                    queued = self.queue('send', **second)
                    route_write = self.queue('register', project='demo', name='delta', cwd=str(self.root))
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(timed_out.readline(), .05)
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(watcher.readline(), .05)
                    self.assertFalse(queued.done())
                    self.assertFalse(route_write.done())
                    self.assertNotIn('delta', [route['name'] for route in await self.call('routes', project='demo')])
                    self.assertEqual(self.saved_messages(), [])
                replay = self.queue('send', **first)
                release.set()
                accepted, duplicate, registered = await asyncio.gather(queued, replay, route_write)
            self.assertEqual(registered['name'], 'delta')
            event = json.loads(await asyncio.wait_for(watcher.readline(), 5))
            self.assertEqual(event['message']['id'], duplicate['id'])
            self.assertEqual([m['id'] for m in self.saved_messages()], [duplicate['id'], accepted['id']])
            after = await self.call('ping')
            self.assertEqual(after['messages'], 2)
            self.assertGreaterEqual(after['checkpoint_max_save_seconds'], 1.5)
        await self.call('shutdown')
        await asyncio.wait_for(serving, 5)
        await self.start()
        self.assertEqual((await self.call('send', **first))['id'], duplicate['id'])
        self.assertEqual((await self.call('send', **second))['id'], accepted['id'])

    async def test_checkpoint_commits_while_default_executor_is_occupied(self):
        await self.start()
        await self.register()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        started = asyncio.Event()
        release = threading.Event()

        def occupy_default_worker():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise AssertionError('Test did not release the default worker')

        worker = loop.run_in_executor(None, occupy_default_worker)
        try:
            await asyncio.wait_for(started.wait(), 5)
            sent = await asyncio.wait_for(self.call('send', **self.send_fields('isolated')), 2)
            self.assertFalse(worker.done())
            self.assertEqual([message['id'] for message in self.saved_messages()], [sent['id']])
        finally:
            release.set()
            await worker

    async def test_failed_checkpoint_has_no_ack_event_or_memory_publication(self):
        await self.start()
        await self.register()
        before = await self.call('ping')
        async with self.open_request('watch', project='demo', name='beta') as watcher:
            await asyncio.wait_for(watcher.readline(), 5)
            with self.pause_next_write(fail=True) as (started, release):
                sending = self.queue('send', **self.send_fields('write-error'))
                await asyncio.wait_for(started.wait(), 5)
                self.assertFalse(sending.done())
                release.set()
                with self.assertRaisesRegex(ValueError, 'fixture checkpoint write failed'):
                    await sending
            after = await self.call('ping')
            self.assertEqual(after['messages'], 0)
            self.assertEqual(after['checkpoint_bytes'], before['checkpoint_bytes'])
            self.assertEqual(self.saved_messages(), [])
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(watcher.readline(), .05)
            sent = await self.call('send', **self.send_fields('write-error'))
            event = json.loads(await asyncio.wait_for(watcher.readline(), 5))
            self.assertEqual(event['message']['id'], sent['id'])

    async def test_shutdown_drains_admitted_commit_before_unlocking(self):
        serving = await self.start()
        await self.register()
        first = self.send_fields('admitted')
        queued = self.send_fields('not-admitted', recipient='gamma')
        with self.pause_next_write() as (started, release):
            async with self.open_request('send', **first) as reader:
                await asyncio.wait_for(started.wait(), 5)
                async with self.open_request('send', **queued) as next_reader:
                    self.assertEqual(await self.call('shutdown'), {'stopping': True})
                    self.assertEqual(await asyncio.wait_for(reader.readline(), 5), b'')
                    self.assertEqual(await asyncio.wait_for(next_reader.readline(), 5), b'')
                self.assertFalse(serving.done())
                with self.path.with_suffix('.lock').open('a') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, 'already in use'):
                    route_store.RouteStore(self.state_dir)
                self.assertEqual(self.saved_messages(), [])
                release.set()
                await asyncio.wait_for(serving, 5)
        original, = self.saved_messages()
        self.assertEqual(original['body'], first['body'])
        await self.start()
        self.assertEqual((await self.call('send', **first))['id'], original['id'])
        self.assertEqual((await self.call('ping'))['messages'], 1)

    async def test_wait_and_inbox_registration_see_the_published_checkpoint(self):
        await self.start()
        routes = await self.register()
        evicted = await self.call('send', **self.send_fields('evicted'))
        await self.call('resolve', id=evicted['id'], state='done', note='Retirable fixture')
        first = await self.call('send', **self.send_fields('first'))
        second = await self.call('send', **self.send_fields('second'))
        inbox = dict(owner='a' * 64, context='b' * 64,
                     routes=[{'slot': 'test-slot', 'route': routes['beta']}])
        with self.pause_next_write() as (started, release):
            sending = self.queue('send', **self.send_fields('third'))
            await asyncio.wait_for(started.wait(), 5)
            waiting = self.queue('wait', id=evicted['id'], timeout=5)
            listing = self.queue('inbox_list', limit=1, **inbox)
            getting = self.queue('inbox_get', id=first['id'], **inbox)
            await asyncio.wait((waiting, listing, getting), timeout=.05)
            self.assertTrue(all(not task.done() for task in (waiting, listing, getting)))
            self.assertEqual((await self.call('retention-plan'))['active_waits'], 0)
            release.set()
            third = await sending
            with self.assertRaisesRegex(ValueError, 'Unknown message'):
                await waiting
            page, payload = await asyncio.gather(listing, getting)
        self.assertEqual(payload['body'], first['body'])
        self.assertTrue(page['has_more'])
        next_page = await self.call('inbox_list', snapshot=page['snapshot'], offset=page['offset'], **inbox)
        self.assertEqual([m['id'] for m in page['messages'] + next_page['messages']],
                         [first['id'], second['id'], third['id']])

    async def test_crash_before_payload_commit_keeps_previously_acknowledged_message(self):
        # Stop just before the payload write, after the index write. This is a
        # real process crash, so no graceful-shutdown drain can finish the send.
        script = """
import asyncio, json, pathlib, sys, threading
import broker, message_store

path, state = map(pathlib.Path, sys.argv[1:])
write = message_store.atomic_write

def pause(target, data, *, measure=None):
    if target.name == 'correspondence.json' and any(
            item['body'] == 'interrupted' for item in json.loads(data)['messages']):
        print('paused', flush=True)
        if not threading.Event().wait(15):
            raise OSError('test checkpoint was never interrupted')
    write(target, data, measure=measure)

message_store.atomic_write = pause
asyncio.run(broker.serve(path, state_dir=state, usage_interval=0,
                         codex_failure_interval=0, agy_failure_interval=0))
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c', script, str(self.path), str(self.state_dir),
            cwd=str(Path(broker.__file__).parent), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
            self.assertTrue(ready['ready'])
            await self.register()
            accepted = await self.call('send', **self.send_fields('accepted'))
            async with self.open_request('send', **self.send_fields('interrupted')) as reader:
                self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b'paused\n')
                self.assertEqual([m['id'] for m in self.saved_messages()], [accepted['id']])
                process.kill()
                self.assertEqual(await asyncio.wait_for(reader.readline(), 5), b'')
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(process.communicate(), 5)
        await self.start()
        self.assertEqual((await self.call('send', **self.send_fields('accepted')))['id'], accepted['id'])
        self.assertEqual((await self.call('ping'))['messages'], 1)
        interrupted = await self.call('send', **self.send_fields('interrupted'))
        self.assertEqual([m['id'] for m in self.saved_messages()], [accepted['id'], interrupted['id']])
