"""A push that never reports leaves `dispatching` by name, bounded (issue #153)."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import broker
import route_store


def stamp(seconds_ago, base):
    return datetime.fromtimestamp(base - seconds_ago, timezone.utc).isoformat()


class DispatchBoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-dispatch-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.store = route_store.RouteStore(Path(self.temp.name) / 'state')
        self.addCleanup(self.store.close)
        self.state = broker.State(store=self.store, max_messages=32)
        self.sid = str(uuid.uuid4())
        for name, sid in (('worker', self.sid), ('other', str(uuid.uuid4())), ('third', str(uuid.uuid4())),
                          ('fourth', str(uuid.uuid4())), ('fifth', str(uuid.uuid4()))):
            self.call('register', project='p', name=name, session=sid, cwd=self.temp.name, transport='agy')
        self.route = deepcopy(self.state.route('p', 'worker'))

    def call(self, op, **args):
        return self.state.action(dict(op=op, **args))

    def send(self, key, sender='other', recipient='worker'):
        return self.call('send', project='p', sender=sender, recipient=recipient, body=key, key=key)

    def dispatching(self, key, sender='other', recipient='worker', **claim):
        message = self.send(key, sender, recipient)
        self.call('claim', id=message['id'], **claim)
        return self.state.messages[message['id']]

    def started(self, message):
        return datetime.fromisoformat(message['dispatch_started_at']).timestamp()

    def expire_at(self, current):
        with patch.object(broker.time, 'time', return_value=current):
            return self.call('dispatch-expire')

    def test_bound_moves_only_dispatching_to_uncertain_after_the_limit(self):
        stuck = self.dispatching('stuck')
        pending = self.send('pending', 'third', 'fourth')
        submitted = self.dispatching('submitted', 'third', 'other')
        self.call('finish', id=submitted['id'], state='submitted', event='pushed')
        uncertain = self.dispatching('uncertain', 'fourth', 'fifth')
        self.call('finish', id=uncertain['id'], state='uncertain', note='fixture')
        untouched = {mid: deepcopy(self.state.messages[mid]) for mid in (pending['id'], submitted['id'], uncertain['id'])}
        base = self.started(stuck)

        before = self.state.correspondence.path.read_bytes()
        self.assertEqual(self.expire_at(base + broker.DISPATCH_LIMIT - 1), {'expired': 0})
        self.assertEqual(self.state.messages[stuck['id']]['state'], 'dispatching')
        # An idle tick writes no checkpoint.
        self.assertEqual(self.state.correspondence.path.read_bytes(), before)

        self.assertEqual(self.expire_at(base + broker.DISPATCH_LIMIT + 1), {'expired': 1})
        moved = self.state.messages[stuck['id']]
        self.assertEqual(moved['state'], 'uncertain')
        self.assertEqual(moved['note'], 'Dispatch outcome was not reported within 600 s; '
                                        'check the receiving session before resolving')
        self.assertEqual(moved['events'][-1]['event'], 'push_failed')
        self.assertEqual(moved['events'][-1]['state'], 'uncertain')
        self.assertNotIn('dispatch_claim', moved)
        for mid, expected in untouched.items():
            self.assertEqual(self.state.messages[mid], expected)
        # Never a resend: a later tick does not move the uncertain message again.
        self.assertEqual(self.expire_at(base + 10 * broker.DISPATCH_LIMIT), {'expired': 0})
        self.assertEqual(self.state.messages[stuck['id']]['events'], moved['events'])

    def test_expiry_is_published_and_saved_and_restore_never_keeps_dispatching(self):
        stuck = self.dispatching('stuck')
        base = self.started(stuck)
        sequence = self.state.sequence
        self.expire_at(base + broker.DISPATCH_LIMIT + 1)
        self.assertEqual(self.state.events[-1]['message']['state'], 'uncertain')
        self.assertGreater(self.state.sequence, sequence)
        # The checkpoint keeps the state. Notes and events are volatile by
        # design (message_store never serializes them), so only state survives.
        with patch('sys.stderr'):
            restored = broker.State(store=self.store, max_messages=32)
        self.assertEqual(restored.messages[stuck['id']]['state'], 'uncertain')

        # A checkpoint holding an old `dispatching` record (broker died mid-push)
        # is already uncertain on restore; the bound has nothing left to move.
        old = self.dispatching('old', 'third', 'fourth')
        self.state.correspondence.save(self.state)
        self.assertIn(b'"dispatching"', self.state.correspondence.path.read_bytes())
        with patch('sys.stderr'):
            restored = broker.State(store=self.store, max_messages=32)
        self.assertEqual(restored.messages[old['id']]['state'], 'uncertain')
        with patch.object(broker.time, 'time', return_value=self.started(old) + broker.DISPATCH_LIMIT + 1):
            self.assertEqual(restored.action(dict(op='dispatch-expire')), {'expired': 0})

    def test_wall_clock_age_falls_back_to_the_entering_event(self):
        stuck = self.dispatching('legacy')
        base = self.started(stuck)
        stuck.pop('dispatch_started_at')
        self.assertEqual(self.expire_at(base + broker.DISPATCH_LIMIT + 1), {'expired': 1})
        unreadable = self.dispatching('unreadable', 'third', 'fourth')
        unreadable['dispatch_started_at'] = 'not a time'
        unreadable['events'] = []
        self.assertEqual(self.expire_at(base + 100 * broker.DISPATCH_LIMIT), {'expired': 0})

    def test_late_finish_after_expiry_cannot_settle_the_message(self):
        unguarded = self.dispatching('unguarded')
        guarded = self.dispatching('guarded', 'third', 'fourth', guarded=True)
        token = guarded['dispatch_claim']
        self.expire_at(max(self.started(unguarded), self.started(guarded)) + broker.DISPATCH_LIMIT + 1)
        # A guarded sender holds a claim token the expiry cleared.
        with self.assertRaisesRegex(ValueError, 'Delivery claim changed'):
            self.call('finish', id=guarded['id'], state='submitted', event='pushed', expect_dispatch_claim=token)
        # An unguarded sender (Codex, Antigravity) gets the current record back unchanged.
        events = deepcopy(self.state.messages[unguarded['id']]['events'])
        result = self.call('finish', id=unguarded['id'], state='submitted', event='pushed')
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(self.state.messages[unguarded['id']]['events'], events)
        for mid in (unguarded['id'], guarded['id']):
            self.assertEqual(self.state.messages[mid]['state'], 'uncertain')
            self.assertEqual(self.state.messages[mid]['note'], broker.DISPATCH_EXPIRED_NOTE)

    def test_archive_check_names_the_stuck_delivery_then_retains_it_after_expiry(self):
        stuck = self.dispatching('stuck')
        base = self.started(stuck)
        with patch.object(broker.time, 'time', return_value=base + 45):
            with self.assertRaises(ValueError) as caught:
                self.call('archive-correspondence', expected_route=self.route, check_only=True)
        self.assertEqual(str(caught.exception),
                         f"{broker.ARCHIVE_BLOCKED_ERROR}: 1 delivery still dispatching (id {stuck['id']}, 45 s); "
                         'it becomes uncertain after 600 s')
        self.expire_at(base + broker.DISPATCH_LIMIT + 1)
        self.assertEqual(self.call('archive-correspondence', expected_route=self.route, check_only=True),
                         dict(supported=True, matched=1, retained=1))
        self.assertEqual(self.call('archive-correspondence', expected_route=self.route, archived=True),
                         dict(removed=0, retained=1, complete=False, active_retained=0))
        self.assertIn(stuck['id'], self.state.messages)

    def test_archive_refusal_still_names_waits(self):
        waiting = self.send('waiting')
        self.state.waiters[waiting['id']] = 1
        with self.assertRaisesRegex(ValueError, r'blocked by an active delivery, provider continuation or wait: '
                                                r'1 wait or provider continuation$'):
            self.call('archive-correspondence', expected_route=self.route, check_only=True)
        self.state.waiters.clear()

    def test_ping_attention_counts_both_sides_of_the_hour(self):
        current = float(int(time.time()))
        old = self.send('old')
        young = self.send('young', 'third', 'fourth')
        self.state.messages[old['id']]['created'] = stamp(3601, current)
        self.state.messages[young['id']]['created'] = stamp(3599, current)
        stuck = self.dispatching('stuck', 'fourth', 'fifth')
        stuck['dispatch_started_at'] = stamp(120, current)
        unsure = self.dispatching('unsure', 'fifth', 'other')
        self.call('finish', id=unsure['id'], state='uncertain', note='fixture')
        with patch.object(broker.time, 'time', return_value=current):
            ping = self.call('ping')
        self.assertEqual(ping['attention'], {'pending_over_hour': 1, 'oldest_pending_seconds': 3601,
                                             'dispatching': 1, 'oldest_dispatching_seconds': 120,
                                             'uncertain': 1})
        self.state.messages[old['id']]['created'] = stamp(3599, current)
        with patch.object(broker.time, 'time', return_value=current):
            self.assertEqual(self.call('ping')['attention']['pending_over_hour'], 0)
        empty = broker.State()
        self.assertEqual(empty.action(dict(op='ping'))['attention'],
                         {'pending_over_hour': 0, 'oldest_pending_seconds': None, 'dispatching': 0,
                          'oldest_dispatching_seconds': None, 'uncertain': 0})
        text = repr(ping['attention'])
        for secret in (old['id'], 'worker', self.sid):
            self.assertNotIn(secret, text)


class DispatchLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_serialized_expiry_commits_through_the_checkpoint(self):
        temp = tempfile.TemporaryDirectory(prefix='ab-dispatch-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        store = route_store.RouteStore(Path(temp.name) / 'state')
        self.addCleanup(store.close)
        state = broker.State(store=store)
        for name in ('worker', 'other'):
            state.action(dict(op='register', project='p', name=name, session=str(uuid.uuid4()),
                              cwd=temp.name, transport='agy'))
        message = state.action(dict(op='send', project='p', sender='other', recipient='worker', body='b', key='k'))
        state.action(dict(op='claim', id=message['id']))
        started = datetime.fromisoformat(state.messages[message['id']]['dispatch_started_at']).timestamp()
        before = state.correspondence.path.read_bytes()
        with patch.object(broker.time, 'time', return_value=started + broker.DISPATCH_LIMIT + 1):
            self.assertEqual(await state.action_async({'op': 'dispatch-expire'}), {'expired': 1})
        self.assertEqual(state.messages[message['id']]['state'], 'uncertain')
        after = state.correspondence.path.read_bytes()
        self.assertNotEqual(after, before)
        self.assertNotIn(b'"dispatching"', after)


    async def test_loop_expires_and_survives_failures(self):
        state = broker.State()
        calls = []

        async def action(args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError('disk failed')
            return {'expired': 0}

        state.action_async = action
        with patch('sys.stderr'):
            task = asyncio.create_task(broker.dispatch_loop(state, interval=0))
            while len(calls) < 3:
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual({call['op'] for call in calls}, {'dispatch-expire'})


if __name__ == '__main__':
    unittest.main()
