"""Failure-only fixtures. Provider reads are stubbed and never run a turn."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import broker
import codex_live
import provider_failures
import route_store
from support import RunningBroker
from test_codex_new import ModelDesktop


FIXTURE = Path(__file__).with_name('fixtures') / 'codex-provider-failure.json'


def failure_snapshot():
    return json.loads(FIXTURE.read_text())


def fixture_turn(snapshot):
    return snapshot['turnHistory']['history']['entitiesByKey']['turn-key']


class DetectionTests(unittest.TestCase):
    def test_completed_tool_work_does_not_hide_terminal_failure(self):
        self.assertEqual(provider_failures.terminal_failure(failure_snapshot()),
                         {'kind': 'responseStreamDisconnected',
                          'failed_at': '1970-01-01T00:00:03+00:00', 'turn_id': 'failed-turn'})

    def test_only_final_correlated_failure_is_a_signal(self):
        changes = [
            lambda s: s.update(threadRuntimeStatus={'type': 'active'}),
            lambda s: fixture_turn(s).update(status='completed'),
            lambda s: fixture_turn(s).update(turnId=None),
            lambda s: fixture_turn(s)['items'][-1].update(willRetry=True),
            lambda s: fixture_turn(s)['items'][-1].pop('willRetry'),
            lambda s: fixture_turn(s).update(items=[]),
            lambda s: fixture_turn(s).update(error=None),
            lambda s: fixture_turn(s)['error'].update(codexErrorInfo='usageLimitExceeded'),
            lambda s: s['turnHistory']['history']['islands'][0].update(newerBoundary={'status': 'unknown'}),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                snapshot = failure_snapshot()
                change(snapshot)
                self.assertIsNone(provider_failures.terminal_failure(snapshot))
        self.assertIsNone(provider_failures.terminal_failure({}))

    def test_unknown_nontransient_and_native_capacity_kinds_are_not_network_failures(self):
        for kind in ('usageLimitExceeded', 'unauthorized', 'serverOverloaded',
                     'policyCapacityUnavailable', 'newProviderError'):
            self.assertIsNone(provider_failures.transient_kind(kind))
        for status in (400, 401, 403, 404, True, '503'):
            self.assertIsNone(provider_failures.transient_kind({'httpConnectionFailed': {'httpStatusCode': status}}))
        for status in (408, 429, 500, 503):
            self.assertEqual(provider_failures.transient_kind({'httpConnectionFailed': {'httpStatusCode': status}}),
                             'httpConnectionFailed')
        self.assertEqual(provider_failures.transient_kind('internalServerError'), 'internalServerError')

    def test_missing_time_stays_unavailable_and_legacy_turns_work(self):
        snapshot = failure_snapshot()
        turn = fixture_turn(snapshot)
        turn.pop('durationMs')
        snapshot.update(turns=[turn])
        snapshot.pop('turnHistory')
        self.assertIsNone(provider_failures.terminal_failure(snapshot)['failed_at'])
        snapshot['resumeState'] = 'resuming'
        self.assertIsNone(provider_failures.terminal_failure(snapshot))

    def test_exact_pinned_owner_and_read_only_methods(self):
        client_type = codex_live.Client
        with ModelDesktop() as desktop:
            session = failure_snapshot()['id']
            snapshot = failure_snapshot()
            # ModelDesktop supplies the actual versioned snapshot frame through
            # the same parser used by production. Only its state is a fixture.
            def snapshot_read(client, sid, owner):
                self.assertEqual(owner, desktop.owner)
                state = original_snapshot(client, sid, owner)
                return dict(state, **snapshot)
            original_snapshot = client_type.snapshot
            with patch.object(codex_live, 'Client', side_effect=lambda **kw: client_type(desktop.path, **kw)), \
                    patch.object(client_type, 'snapshot', snapshot_read):
                result = provider_failures.read_codex_failure(session)
            self.assertEqual(result['kind'], 'responseStreamDisconnected')
            methods = {call.get('method') for call in desktop.calls}
            self.assertEqual(methods, {'initialize', 'thread-owner-discovery', 'thread-stream-following-changed'})


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-failure-')
        self.addCleanup(self.temp.cleanup)
        self.store = route_store.RouteStore(Path(self.temp.name) / 'state')
        self.addCleanup(self.store.close)
        self.state = broker.State(store=self.store)
        self.session = str(uuid.uuid4())
        for name, transport, sid in (('parent', 'realtime', None), ('worker', 'codex', self.session)):
            self.state.action(dict(op='register', project='demo', name=name, transport=transport,
                                   session=sid, cwd=self.temp.name))
        self.source = self.state.action(dict(op='send', project='demo', sender='parent',
                                            recipient='worker', body='PRIVATE_TASK', key='task'))
        self.state.action(dict(op='claim', id=self.source['id']))
        self.state.action(dict(op='finish', id=self.source['id'], state='submitted', event='pushed'))
        self.route = deepcopy(self.state.endpoints[('demo', 'worker')])
        self.failure = provider_failures.terminal_failure(failure_snapshot())

    def notify(self):
        return self.state.action(dict(op='provider-failure', expected_route=self.route,
                                      source_message_id=self.source['id'], failure=self.failure))

    def test_notice_visible_in_show_cli_and_mcp_inbox_without_answering_task(self):
        notice = self.notify()
        self.assertEqual(self.state.action(dict(op='show', id=notice['id']))['body'], notice['body'])
        inbox = self.state.action(dict(op='inbox', project='demo', name='parent'))
        self.assertEqual([entry['id'] for entry in inbox], [notice['id']])
        routes = [{'slot': 'owner-slot', 'route': self.state.route('demo', name='parent')}]
        inbox = self.state.action(dict(op='inbox_list', owner='a' * 64, context='b' * 64, routes=routes))
        self.assertEqual([entry['id'] for entry in inbox['messages']], [notice['id']])
        self.assertIsNone(notice['reply_to'])
        self.assertEqual(self.state.get(self.source['id'])['state'], 'submitted')
        self.assertFalse(self.state.answers[self.source['id']].is_set())
        self.assertEqual(notice['events'][0]['event'], 'provider_failure_detected')
        self.assertEqual(json.loads(notice['body'])['failed_at'], self.failure['failed_at'])
        saved = self.state.correspondence.path.read_text()
        for private in ('PRIVATE_OWNER', 'PRIVATE_PROVIDER_DIAGNOSTIC', 'PRIVATE_TOOL_OUTPUT'):
            self.assertNotIn(private, saved)

    def test_duplicate_is_suppressed_with_retained_correspondence_after_restart(self):
        first = self.notify()
        with patch.object(route_store, 'verify_route', return_value=None):
            restored = broker.State(store=self.store)
        with patch.object(restored.correspondence, 'save', side_effect=AssertionError('duplicate write')):
            second = restored.action(dict(op='provider-failure', expected_route=self.route,
                                          source_message_id=self.source['id'], failure=self.failure))
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(len(restored.messages), 2)

    def test_completed_task_still_notifies_last_sender(self):
        self.state.action(dict(op='resolve', id=self.source['id'], state='done', note='fixture completion'))
        notice = self.notify()
        self.assertEqual(notice['recipient'], 'parent')
        self.assertEqual(self.state.get(self.source['id'])['state'], 'done')

    def test_every_registered_thread_is_observed_even_without_any_sender(self):
        self.state.messages.clear()
        self.state.keys.clear()
        async def run():
            finished = asyncio.Event()
            original_action = self.state.action
            def action(args):
                result = original_action(args)
                finished.set()
                return result
            with patch.object(provider_failures, 'read_codex_failure', return_value=self.failure) as read, \
                    patch.object(self.state, 'action', side_effect=action):
                task = asyncio.create_task(provider_failures.observe_codex_failures(self.state, .001))
                try:
                    await asyncio.wait_for(finished.wait(), 2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                read.assert_called_once_with(self.session)
        asyncio.run(run())
        notice = next(iter(self.state.messages.values()))
        self.assertEqual(notice['recipient'], 'worker')
        self.assertIsNone(json.loads(notice['body'])['source_message_id'])

    def test_new_last_sender_gets_notice_and_notification_does_not_become_sender(self):
        self.state.action(dict(op='register', project='demo', name='other', transport='realtime', cwd=self.temp.name))
        latest = self.state.action(dict(op='send', project='demo', sender='other', recipient='worker',
                                       body='latest task', key='last-sender'))
        notice = self.state.action(dict(op='provider-failure', expected_route=self.route,
                                       source_message_id=latest['id'], failure=self.failure))
        self.assertEqual(notice['recipient'], 'other')
        self.assertEqual(provider_failures.last_sender_message(self.state, self.route)['id'], latest['id'])

    def test_durable_write_failure_keeps_watch_and_publishes_nothing(self):
        before = deepcopy(self.state.messages)
        sequence = self.state.sequence
        with patch.object(self.state.correspondence, 'save', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                self.notify()
        self.assertEqual(self.state.messages, before)
        self.assertEqual(self.state.sequence, sequence)
        self.assertEqual(provider_failures.last_sender_message(self.state, self.route)['id'], self.source['id'])

    def test_remap_and_newer_delivery_refuse_old_observation(self):
        self.state.endpoints[('demo', 'worker')]['session'] = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.notify()
        self.state.endpoints[('demo', 'worker')]['session'] = self.session
        self.state.action(dict(op='send', project='demo', sender='parent', recipient='worker',
                               body='newer input', key='newer'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.notify()

    def test_observer_normal_path_and_race_use_fixtures_only(self):
        async def run(race=False):
            finished = asyncio.Event()
            loop = asyncio.get_running_loop()
            original_action = self.state.action
            def action(args):
                result = original_action(args)
                finished.set()
                return result
            def read(*args):
                if args[0] != self.session:
                    loop.call_soon_threadsafe(finished.set)
                    return None
                if race:
                    self.state.endpoints[('demo', 'worker')] = dict(self.route, session=str(uuid.uuid4()))
                return self.failure
            with patch.object(provider_failures, 'read_codex_failure', side_effect=read), \
                    patch.object(self.state, 'action', side_effect=action):
                task = asyncio.create_task(provider_failures.observe_codex_failures(self.state, .001))
                try:
                    await asyncio.wait_for(finished.wait(), 2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run(race=True))
        self.assertEqual(len(self.state.messages), 1)
        self.state.endpoints[('demo', 'worker')] = self.route
        asyncio.run(run())
        self.assertEqual(len(self.state.messages), 2)

    def test_socket_entrypoint_posts_normalized_notice(self):
        with RunningBroker() as server:
            for name, transport, sid in (('parent', 'realtime', None), ('worker', 'codex', self.session)):
                server.call('register', project='demo', name=name, transport=transport,
                            session=sid, cwd=server.temp.name)
            source = server.call('send', project='demo', sender='parent', recipient='worker', body='task', key='socket-task')
            server.call('claim', id=source['id'])
            server.call('finish', id=source['id'], state='submitted', event='pushed')
            route = server.call('routes', project='demo')[1]
            notice = server.call('provider-failure', expected_route=route,
                                 source_message_id=source['id'], failure=self.failure)
            self.assertEqual(server.call('inbox', project='demo', name='parent')[0]['id'], notice['id'])
            # Route verification during restart must not inspect a native app.
            server.call('unregister', project='demo', name='worker')
            server.restart()
            self.assertEqual(server.call('show', id=notice['id'])['body'], notice['body'])
