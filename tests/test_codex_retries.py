"""Codex continuation fixtures. Every Desktop, clock and provider read is isolated."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import broker
import codex_failures as adapter
import codex_live
import message_store
import provider_failures as shared
import route_store
from test_codex_live import FakeDesktop
import test_provider_failures as detection_fixtures

failure_snapshot = detection_fixtures.failure_snapshot
fixture_turn = detection_fixtures.fixture_turn


CLIENT = codex_live.Client


def snapshot(turn_id='failed-turn', status='failed'):
    value = failure_snapshot()
    value.update(requests=[], latestTurnStartMessageId='fixture-input')
    fixture_turn(value).update(turnId=turn_id, status=status, completedItemIds=[])
    return value


class RecoveryDesktop(FakeDesktop):
    def __init__(self, state, behavior='active', before_write=None):
        super().__init__(behavior)
        self.state, self.before_write = state, before_write

    def handle(self, sock, request):
        if request.get('method') == 'thread-stream-following-changed':
            self.calls.append(request)
            if request['params']['following']:
                value = dict(self.state, id=request['params']['conversationId'])
                self.write(sock, dict(type='broadcast', method='thread-stream-state-changed', version=11,
                    sourceClientId=self.owner, targetClientIds=[request['sourceClientId']],
                    params=dict(hostId='local', conversationId=value['id'],
                                change=dict(type='snapshot', revision=1, conversationState=value))))
            return True
        if request.get('method') == 'thread-follower-start-turn' and self.before_write:
            self.before_write()
        return super().handle(sock, request)


class GuardTests(unittest.TestCase):
    def refuse(self, value):
        return adapter.retry_refusal(value, 'failed-turn', adapter.snapshot_fingerprint(snapshot()))

    def test_completed_calls_are_allowed_but_started_without_output_is_refused(self):
        self.assertIsNone(self.refuse(snapshot()))
        for kind in ('commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall', 'collabAgentToolCall'):
            value = snapshot()
            fixture_turn(value)['items'][0] = dict(type=kind, status='inProgress', id='call')
            self.assertEqual(self.refuse(value), 'started_without_output')
        value = snapshot()
        fixture_turn(value)['items'][0] = dict(type='function_call', call_id='unmatched-call')
        self.assertEqual(self.refuse(value), 'started_without_output')
        fixture_turn(value)['items'].append(dict(type='function_call_output', call_id='unmatched-call', output='done'))
        fingerprint = adapter.snapshot_fingerprint(value)
        self.assertIsNone(adapter.retry_refusal(value, 'failed-turn', fingerprint))

    def test_newer_turn_input_native_retry_and_incomplete_items(self):
        value = snapshot('newer-turn')
        self.assertEqual(self.refuse(value), 'newer_turn_or_missing_history')
        value = snapshot()
        fixture_turn(value)['params'] = {'input': [{'type': 'text', 'text': 'new input'}]}
        self.assertEqual(self.refuse(value), 'intervening_input_or_changed_turn')
        value = snapshot()
        value['requests'] = [{'id': 'pending'}]
        self.assertEqual(self.refuse(value), 'pending_input_or_native_retry')
        value = snapshot()
        fixture_turn(value)['items'][-1]['willRetry'] = True
        self.assertEqual(self.refuse(value), 'native_retry_or_nonterminal_turn')
        value = snapshot()
        fixture_turn(value)['itemsPagination'] = {'hasLoadedOldest': False}
        self.assertEqual(self.refuse(value), 'incomplete_tool_history')

    def test_native_payload_and_reservation_precede_single_write(self):
        value = snapshot()
        reserved = []
        with RecoveryDesktop(value, before_write=lambda: self.assertEqual(reserved, [True])) as desktop:
            with patch.object(codex_live, 'Client', side_effect=lambda **kw: CLIENT(desktop.path, **kw)):
                result = adapter.resume_failed_turn(value['id'], desktop.owner, 'failed-turn',
                    adapter.snapshot_fingerprint(value), lambda: reserved.append(True))
        self.assertEqual(result, 'idle-turn')
        starts = [call for call in desktop.calls if call.get('method') == 'thread-follower-start-turn']
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['version'], 2)
        self.assertEqual(starts[0]['targetClientId'], desktop.owner)
        self.assertEqual(starts[0]['params'], dict(conversationId=value['id'], turnStart=dict(
            request=dict(threadId=value['id'], input=[], turnTrigger='resume_interrupted_task'),
            context=dict(inheritThreadSettings=True, writingBlockContextPrepared=True))))

    def test_owner_change_and_reservation_failure_write_nothing(self):
        value = snapshot()
        with RecoveryDesktop(value) as desktop:
            with patch.object(codex_live, 'Client', side_effect=lambda **kw: CLIENT(desktop.path, **kw)):
                with self.assertRaises(adapter.RetryRefused):
                    adapter.resume_failed_turn(value['id'], 'different-owner', 'failed-turn',
                                               adapter.snapshot_fingerprint(value), lambda: self.fail('reserved'))
                def fail_reservation():
                    raise OSError('fixture reservation failure')
                with self.assertRaises(codex_live.DeliveryError) as raised:
                    adapter.resume_failed_turn(value['id'], desktop.owner, 'failed-turn',
                                               adapter.snapshot_fingerprint(value), fail_reservation)
                self.assertFalse(raised.exception.uncertain)
        self.assertFalse(any(call.get('method') == 'thread-follower-start-turn' for call in desktop.calls))


class RetryTests(unittest.TestCase):
    def setUp(self):
        detection_fixtures.NoticeTests.setUp(self)
        self.now = 100
        self.monitor = adapter.RetryMonitor(self.state, shared.retry_policy(), clock=lambda: self.now)
        self.sent = []

    def root(self):
        return adapter.retry_notice(self.state, self.route)

    def control(self):
        return self.root()['provider_retry']

    def step(self, value=None, send=None, owner='fixture-owner', route=None):
        value = snapshot() if value is None else value
        def accepted(session, pinned, turn_id, fingerprint, reserve):
            reserve()
            saved = json.loads(self.state.correspondence.path.read_text())
            record = next(message for message in saved['messages'] if message.get('provider_retry'))
            self.assertEqual(record['provider_retry']['status'], 'reserved')
            self.sent.append(turn_id)
            return 'retry-turn-' + str(len(self.sent))
        with patch.object(adapter, 'read_codex_observation', return_value=dict(owner=owner, snapshot=deepcopy(value))), \
                patch.object(adapter, 'resume_failed_turn', side_effect=send or accepted):
            asyncio.run(self.monitor.check_route(route or self.route))

    def test_backoff_reservation_and_event_share_detection_conversation(self):
        self.step()
        root = self.root()
        original_body = root['body']
        self.assertEqual(self.control()['next_at'], 115)
        self.now = 114
        self.step()
        self.assertEqual(self.sent, [])
        self.now = 115
        self.step()
        self.assertEqual(self.sent, ['failed-turn'])
        self.assertEqual(self.control()['attempts'], 1)
        self.assertEqual(self.control()['status'], 'accepted')
        events = self.state.action(dict(op='conversation', id=root['id']))
        retry = [message for message in events if json.loads(message['body']).get('event') == shared.RETRY_EVENT]
        self.assertEqual(len(retry), 1)
        body = json.loads(retry[0]['body'])
        self.assertEqual((body['attempt'], body['cap']), (1, 3))
        self.assertEqual(self.root()['body'], original_body)
        self.assertEqual(self.root()['state'], 'pending')
        self.assertEqual(self.state.get(self.source['id'])['state'], 'submitted')

    def test_cap_three_then_named_exhaustion_and_no_fourth_send(self):
        current = snapshot()
        self.step(current)
        for attempt, delay in enumerate((15, 30, 60), 1):
            self.assertEqual(self.control()['next_at'], self.now + delay)
            self.now += delay
            self.step(current)
            self.assertEqual(self.control()['attempts'], attempt)
            current = snapshot('retry-turn-' + str(attempt))
            self.now += 1
            self.step(current)
        self.assertEqual(self.control()['status'], 'exhausted')
        for _ in range(3):
            self.now += 100
            self.step(current)
        self.assertEqual(len(self.sent), 3)
        events = self.state.action(dict(op='conversation', id=self.root()['id']))
        exhausted = [json.loads(message['body']) for message in events
                     if json.loads(message['body']).get('event') == shared.EXHAUSTED]
        self.assertEqual(len(exhausted), 1)
        self.assertEqual((exhausted[0]['attempt'], exhausted[0]['cap']), (3, 3))

    def test_counter_resets_after_accepted_turn_completes_nonfailed(self):
        self.step()
        self.now = 115
        self.step()
        self.assertEqual(self.control()['attempts'], 1)
        self.step(snapshot('retry-turn-1', 'completed'))
        self.assertEqual(self.control()['status'], 'recovered')
        self.assertEqual(self.control()['attempts'], 0)

        old_root = self.root()['id']
        self.now += 5
        self.step(snapshot('later-failure'))
        self.assertNotEqual(self.root()['id'], old_root)
        self.assertEqual(self.control()['next_at'], self.now + 15)
        self.assertEqual(self.control()['attempts'], 0)

    def test_aliases_of_the_same_thread_share_the_cap(self):
        self.monitor = adapter.RetryMonitor(self.state, shared.retry_policy((1,), 1), clock=lambda: self.now)
        alias = self.state.action(dict(op='register', project='demo', name='alias', transport='codex',
                                      session=self.session, cwd=self.temp.name))
        self.step()
        original = self.root()['id']
        self.now += 1
        self.step(route=alias)
        self.assertEqual(adapter.retry_notice(self.state, alias)['id'], original)
        self.assertEqual(self.control()['attempts'], 1)
        self.step(snapshot('retry-turn-1'), route=alias)
        self.step(snapshot('retry-turn-1'))
        self.assertEqual(self.control()['status'], 'exhausted')
        self.assertEqual(len(self.sent), 1)

    def test_newer_turn_between_detection_and_attempt_is_refused(self):
        old = snapshot()
        with RecoveryDesktop(snapshot('newer-turn')) as desktop:
            self.step(old, owner=desktop.owner)
            self.now = 115
            with patch.object(adapter, 'read_codex_observation', return_value=dict(owner=desktop.owner, snapshot=old)), \
                    patch.object(codex_live, 'Client', side_effect=lambda **kw: CLIENT(desktop.path, **kw)):
                asyncio.run(self.monitor.check_route(self.route))
        self.assertEqual(self.control()['status'], 'unsafe')
        self.assertFalse(any(call.get('method') == 'thread-follower-start-turn' for call in desktop.calls))

    def test_ambiguous_write_is_uncertain_and_never_sent_again(self):
        value = snapshot()
        def reserved_on_disk():
            saved = json.loads(self.state.correspondence.path.read_text())
            record = next(message for message in saved['messages'] if message.get('provider_retry'))
            self.assertEqual(record['provider_retry']['status'], 'reserved')
        with RecoveryDesktop(value, behavior='idle-disconnect', before_write=reserved_on_disk) as desktop:
            self.step(value, owner=desktop.owner)
            self.now = 115
            for _ in range(3):
                with patch.object(adapter, 'read_codex_observation', return_value=dict(owner=desktop.owner, snapshot=value)), \
                        patch.object(codex_live, 'Client', side_effect=lambda **kw: CLIENT(desktop.path, **kw)):
                    asyncio.run(self.monitor.check_route(self.route))
                self.now += 100
        self.assertEqual(self.control()['status'], 'uncertain')
        self.assertEqual(self.control()['attempts'], 0)
        self.assertEqual(sum(call.get('method') == 'thread-follower-start-turn' for call in desktop.calls), 1)
        event = self.state.get(self.control()['event_id'])
        self.assertEqual(json.loads(event['body'])['event'], shared.UNCERTAIN)
        self.assertEqual(event['conversation'], self.root()['conversation'])

    def test_intervening_input_owner_and_outstanding_call_block(self):
        for reason in ('input', 'owner', 'call'):
            with self.subTest(reason=reason):
                # Independent notice/session per case keeps the refusal terminal.
                self.setUp()
                self.step()
                value = snapshot()
                owner = 'fixture-owner'
                if reason == 'input':
                    fixture_turn(value)['params'] = {'input': [{'type': 'text', 'text': 'intervening'}]}
                elif reason == 'owner':
                    owner = 'changed-owner'
                else:
                    fixture_turn(value)['items'][0]['status'] = 'inProgress'
                self.now = 115
                self.step(value, owner=owner)
                self.assertEqual(self.control()['status'], 'unsafe')
                self.assertEqual(self.sent, [])

    def test_new_broker_input_prevents_reservation(self):
        self.step()
        self.state.action(dict(op='send', project='demo', sender='parent', recipient='worker',
                               body='new input', key='later-input'))
        self.now = 115
        self.step()
        self.assertEqual(self.control()['status'], 'unsafe')
        self.assertEqual(self.sent, [])

    def test_restart_during_reservation_stops_without_provider_read(self):
        self.step()
        self.now = 115
        self.monitor.action(self.root(), 'reserve', fingerprint=self.control()['fingerprint'])
        with patch.object(route_store, 'verify_route', return_value=None):
            restored = broker.State(store=self.store)
        monitor = adapter.RetryMonitor(restored, shared.retry_policy(), clock=lambda: self.now)
        with patch.object(adapter, 'read_codex_observation', side_effect=AssertionError('provider read')):
            asyncio.run(monitor.check_route(self.route))
        root = adapter.retry_notice(restored, self.route)
        self.assertEqual(root['provider_retry']['status'], 'uncertain')
        self.assertEqual(root['provider_retry']['attempts'], 0)

    def test_reservation_write_failure_is_atomic(self):
        self.step()
        before = deepcopy(self.state.messages)
        self.now = 115
        with patch.object(self.state.correspondence, 'save', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                self.monitor.action(self.root(), 'reserve', fingerprint=self.control()['fingerprint'])
        self.assertEqual(self.state.messages, before)
        self.assertEqual(self.control()['status'], 'waiting')

    def test_cancel_before_reservation_cannot_start_a_late_turn(self):
        self.step()
        self.now = 115
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def send(session, owner, turn_id, fingerprint, reserve):
            entered.set()
            release.wait(2)
            try:
                reserve()
                self.sent.append(turn_id)
            except adapter.RetryRefused:
                pass
            finally:
                finished.set()
            return 'late-turn'
        async def cancel():
            with patch.object(adapter, 'read_codex_observation',
                              return_value=dict(owner='fixture-owner', snapshot=snapshot())), \
                    patch.object(adapter, 'resume_failed_turn', side_effect=send):
                task = asyncio.create_task(self.monitor.check_route(self.route))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        asyncio.run(cancel())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.control()['status'], 'waiting')

    def test_retry_checkpoint_rejects_diagnostics_and_keeps_only_normalized_state(self):
        self.step()
        record = self.control()
        with self.assertRaises(ValueError):
            message_store.retry_record(dict(record, desktop_owner='PRIVATE_OWNER'))
        for field, value in [('status', 'unknown'), ('attempts', True), ('fingerprint', 'bad'),
                             ('next_at', float('nan')), ('reservation_id', 'bad')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                message_store.retry_record(dict(record, **{field: value}))
        saved = self.state.correspondence.path.read_text()
        for private in ('PRIVATE_PROVIDER_DIAGNOSTIC', 'PRIVATE_TOOL_OUTPUT', 'fixture-owner'):
            self.assertNotIn(private, saved)

    def test_lost_checkpoint_stops_in_uncertain_without_starting(self):
        self.step()
        self.state.correspondence.loss = True
        self.now = 115
        self.step()
        self.assertEqual(self.control()['status'], 'uncertain')
        self.assertEqual(self.sent, [])
        event = json.loads(self.state.get(self.control()['event_id'])['body'])
        self.assertEqual(event['kind'], 'correspondence_checkpoint_lost')

    def test_acknowledging_notice_cannot_evict_active_retry_counter(self):
        self.step()
        root_id = self.root()['id']
        self.state.action(dict(op='resolve', id=root_id, state='done', note='fixture owner read notice'))
        self.state.max_messages = 2
        with self.assertRaisesRegex(ValueError, 'no evictable'):
            self.state.action(dict(op='send', project='demo', sender='parent', recipient='worker',
                                   body='capacity probe', key='capacity'))
        self.assertEqual(self.state.get(root_id)['provider_retry']['attempts'], 0)

    def test_bridge_delivery_cannot_interleave_with_reserved_continuation(self):
        self.step()
        self.now = 115
        self.monitor.action(self.root(), 'reserve', fingerprint=self.control()['fingerprint'])
        incoming = self.state.action(dict(op='send', project='demo', sender='parent', recipient='worker',
                                          body='new input', key='during-reservation'))
        with self.assertRaisesRegex(ValueError, 'reserved provider continuation'):
            self.state.action(dict(op='claim', id=incoming['id']))
        self.assertEqual(self.state.get(incoming['id'])['state'], 'pending')
