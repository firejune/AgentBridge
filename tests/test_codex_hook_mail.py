"""Codex peer mail shares the existing PreToolUse boundary with stop."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'
sys.path.insert(0, str(SCRIPTS))
import bridge
import broker
import codex_hook_mail
import codex_stop_hook
import message_store
from delivery import PEER_SCOPE
from support import RunningBroker

THREAD = '00000000-0000-4000-8000-000000000101'
OTHER = '00000000-0000-4000-8000-000000000102'


class HookMailTests(unittest.TestCase):
    def setUp(self):
        self.server = RunningBroker().__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.call('register', project='demo', name='peer', transport='realtime', cwd=self.server.temp.name)
        self.call('register', project='demo', name='target', transport='codex', session=THREAD,
                  cwd=self.server.temp.name)

    def call(self, op, **args):
        return self.server.call(op, **args)

    def message(self, body, *, armed=True, submitted=True, channel=None):
        args = {'channel': channel} if channel else {'recipient': 'target'}
        result = self.call('send', project='demo', sender='peer', body=body, key=f'test-{body[:40]}', **args)
        mid = result['id']
        self.call('claim', id=mid)
        if armed:
            self.assertIs(self.call('codex-queue-arm', id=mid), True)
        if submitted:
            self.call('finish', id=mid, state='submitted', codex_queue_accepted=True)
        return mid

    def hook(self, session=THREAD, *, output=None):
        source = io.BytesIO(json.dumps({'hook_event_name': 'PreToolUse', 'session_id': session}).encode())
        if output is None:
            output = io.StringIO()
        codex_stop_hook.run(self.server.path, source, output)
        if not output.getvalue():
            return None
        decision = json.loads(output.getvalue())['hookSpecificOutput']
        self.assertEqual((decision['hookEventName'], decision['permissionDecision']), ('PreToolUse', 'deny'))
        return decision['permissionDecisionReason']

    def receive(self, mid, *, reread=False, session=THREAD):
        argv = ['--socket', self.server.path, 'receive-codex', mid]
        if reread:
            argv.append('--reread')
        output = io.StringIO()
        with patch.dict(os.environ, {'CODEX_THREAD_ID': session}), redirect_stdout(output):
            status = bridge.main(argv)
        return status, json.loads(output.getvalue())

    def duplicate(self, mid):
        return 0, {'id': mid, 'already_retrieved': True, 'marked_received': False}

    def test_ordered_messages_share_one_denial_and_each_remains_readable_from_the_queue(self):
        first = self.message('Peer correction one')
        second = self.message('Peer correction two')
        self.assertIsNone(self.hook(OTHER))
        self.assertEqual(self.receive(first, session=OTHER), (1, {'error': broker.CODEX_READ_NO_MATCH}))
        reason = self.hook()
        self.assertTrue(reason.startswith('Agent Bridge is offering peer correspondence.'))
        self.assertIn('Act within existing user authorization.', reason)
        self.assertIn('Use its message ID to avoid repeating work.', reason)
        self.assertIn(PEER_SCOPE, reason)
        self.assertIn('peer -> target', reason)
        self.assertLess(reason.index(first), reason.index(second))
        self.assertLess(reason.index('Peer correction one'), reason.index('Peer correction two'))
        self.assertNotIn('End the turn', reason)
        self.assertIsNone(self.hook())
        for mid, body in ((first, 'Peer correction one'), (second, 'Peer correction two')):
            shown = self.call('show', id=mid)
            self.assertEqual((shown['state'], shown['codex_queue_read']), ('submitted', 'ready'))
            self.assertIs(shown['codex_hook_offer_recorded'], True)
            self.assertNotIn('received', [event['state'] for event in shown['events']])
            status, received = self.receive(mid)
            self.assertEqual(status, 0)
            self.assertIn(body, received['text'])
            self.assertIs(received['marked_received'], True)
            self.assertEqual(self.receive(mid), self.duplicate(mid))
            settled = self.call('show', id=mid)
            self.assertEqual([e['note'] for e in settled['events'] if e['state'] == 'received'],
                             [broker.CODEX_READ_FROM_QUEUE])

    def test_completed_hook_reply_leaves_no_queue_payload_or_ack(self):
        mid = self.message('Answer through the bridge')
        self.assertIn(mid, self.hook())
        reply_args = {'id': mid, 'body': 'The answer', 'key': f'reply-{mid}'}
        reply = self.call('reply', **reply_args)
        self.assertEqual(self.call('reply', **reply_args)['id'], reply['id'])
        self.assertEqual([m['id'] for m in self.call('conversation', id=mid)], [mid, reply['id']])
        completed = self.call('show', id=mid)
        self.assertEqual(completed['state'], 'answered')
        self.assert_handled(mid, 'Answer through the bridge')
        self.assertEqual(self.call('show', id=mid), completed)
        self.assertIsNone(self.hook())

    def assert_handled(self, mid, body):
        request = broker.request
        with patch.object(broker, 'request', wraps=request) as calls:
            self.assertEqual(self.receive(mid),
                             (0, {'id': mid, 'already_handled': True, 'marked_received': False}))
            self.assertEqual([call.args[1]['op'] for call in calls.call_args_list], ['codex-queue-read'])
        self.assertEqual(self.receive(mid, session=OTHER), (1, {'error': broker.CODEX_READ_NO_MATCH}))
        with patch.object(broker, 'request', wraps=request) as calls:
            status, reread = self.receive(mid, reread=True)
            self.assertEqual((status, reread['marked_received']), (0, False))
            self.assertIn(body, reread['text'])
            self.assertEqual([call.args[1]['op'] for call in calls.call_args_list], ['show'])
        self.assertEqual(self.receive(mid, reread=True, session=OTHER), (1, {'error': broker.CODEX_READ_NO_MATCH}))

    def test_done_queue_message_stops_even_after_a_prior_queue_read(self):
        mid = self.message('Work completed after queue read')
        self.assertEqual(self.receive(mid)[0], 0)
        self.call('resolve', id=mid, state='done', note='Work complete')
        completed = self.call('show', id=mid)
        self.assertEqual((completed['state'], completed['codex_queue_read']), ('done', 'retrieved'))
        self.assert_handled(mid, 'Work completed after queue read')
        self.assertEqual(self.call('show', id=mid), completed)

    def test_received_and_in_progress_remain_readable_without_a_prior_queue_read(self):
        for op, state in (('ack', 'received'), ('progress', 'in_progress')):
            with self.subTest(state=state):
                body = f'Unsettled message marked {state}'
                mid = self.message(body)
                self.assertIn(mid, self.hook())
                self.call(op, id=mid)
                before = self.call('show', id=mid)
                self.assertEqual((before['state'], before['codex_queue_read']), (state, 'ready'))
                status, result = self.receive(mid)
                self.assertEqual((status, result['marked_received']), (0, False))
                self.assertIn(body, result['text'])
                self.assertEqual(self.call('show', id=mid), before | {'codex_queue_read': 'retrieved'})
                self.assertEqual(self.receive(mid), self.duplicate(mid))

    def test_legacy_done_and_queue_failure_keep_their_diagnostics(self):
        legacy = self.message('Before queue read markers', armed=False, submitted=False)
        self.call('resolve', id=legacy, state='done', note='Work complete')
        failed = self.message('Delivery failed', submitted=False)
        self.call('finish', id=failed, state='failed', note='Delivery failed')
        for mid, state in ((legacy, 'done'), (failed, 'failed')):
            for reread in (False, True):
                with self.subTest(state=state, reread=reread):
                    self.assertEqual(self.receive(mid, reread=reread), (1, {
                        'error': f'The addressed correspondence is already {state}; nothing is left to receive.',
                    }))

    def test_stop_wins_before_proposal_and_while_mail_waits_to_commit(self):
        first = self.message('Peer correction')
        self.call('codex-stop', project='demo', name='target', reason='Take a break')
        self.assertIn('Take a break', self.hook())
        self.assertEqual(self.call('show', id=first)['state'], 'submitted')
        request = broker.request

        def arrive_during_preview(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == codex_hook_mail.CLAIM_OP:
                self.assertEqual(result['kind'], 'mail')
                self.call('codex-stop', project='demo', name='target', reason='Stop now')
            return result

        with patch.object(broker, 'request', side_effect=arrive_during_preview):
            reason = self.hook()
        self.assertIn('End the turn now and wait.', reason)
        self.assertIn('Stop now', reason)
        self.assertNotIn(first, reason)
        self.assertEqual(self.call('show', id=first)['state'], 'submitted')
        self.assertIn(first, self.hook())

    def test_queue_can_win_between_hook_preview_and_atomic_offer(self):
        first = self.message('Already read by queue')
        second = self.message('Still available')
        request = broker.request

        def read_during_preview(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == codex_hook_mail.CLAIM_OP:
                self.assertIn(first, result['reason'])
                self.assertIn(second, result['reason'])
                self.assertIn('Already read by queue', self.receive(first)[1]['text'])
            return result

        with patch.object(broker, 'request', side_effect=read_during_preview):
            self.assertIsNone(self.hook())
        self.assertEqual(self.call('show', id=second)['state'], 'submitted')
        self.assertEqual(self.call('show', id=first)['note'], broker.CODEX_READ_FROM_QUEUE)
        self.assertNotIn(first, self.hook())
        self.assertIn('Still available', self.receive(second)[1]['text'])
        self.assertEqual(self.receive(second), self.duplicate(second))

    def test_overlapping_hook_previews_emit_only_one_denial(self):
        mid = self.message('One offer during overlapping calls')
        request = broker.request

        def overlap(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == codex_hook_mail.CLAIM_OP and result is not None:
                self.assertIsNone(self.hook())
            return result

        with patch.object(broker, 'request', side_effect=overlap):
            self.assertIn(mid, self.hook())
        self.assertIsNone(self.hook())

    def test_oversize_preview_cannot_consume_body(self):
        first = self.message('First body remains queued')
        request = broker.request

        def corrupt(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == codex_hook_mail.CLAIM_OP:
                return {**result, 'reason': result['reason'] + 'x' * codex_hook_mail.MAX_REASON_BYTES}
            self.fail('An invalid preview must never record an offer')

        with patch.object(broker, 'request', side_effect=corrupt):
            self.assertIsNone(self.hook())
        self.assertEqual(self.call('show', id=first)['state'], 'submitted')
        self.assertIn('First body remains queued', self.receive(first)[1]['text'])

    def test_preview_digest_mismatch_cannot_confirm_a_different_body(self):
        first = self.message('Original body stays available')
        request = broker.request

        def corrupt(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == codex_hook_mail.CLAIM_OP:
                return {**result, 'reason': result['reason'].replace('Original body', 'Altered body')}
            return result

        with patch.object(broker, 'request', side_effect=corrupt):
            self.assertIsNone(self.hook())
        self.assertEqual(self.call('show', id=first)['state'], 'submitted')
        self.assertIn('Original body stays available', self.receive(first)[1]['text'])

    def test_lost_offer_response_survives_restart_and_plain_receive_recovers_body(self):
        first = self.message('Offer response lost after durability')
        request = broker.request

        def lose(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == codex_hook_mail.OFFER_OP:
                self.assertEqual(result['kind'], 'mail')
                raise OSError('lost offer response')
            return result

        with patch.object(broker, 'request', side_effect=lose):
            self.assertIsNone(self.hook())
        self.server.restart()
        durable = self.call('show', id=first)
        self.assertEqual(durable['state'], 'submitted')
        self.assertIs(durable['codex_hook_offer_recorded'], True)
        self.assertEqual(durable['codex_queue_read'], 'ready')
        self.assertIsNone(self.hook())
        self.assertIn('Offer response lost after durability', self.receive(first)[1]['text'])
        self.assertEqual(self.receive(first), self.duplicate(first))

    def test_failed_hook_output_keeps_plain_receive_available(self):
        class BrokenOutput(io.StringIO):
            def write(self, value):
                raise BrokenPipeError('hook output closed')

        mid = self.message('Deny output never reached the caller')
        self.assertIsNone(self.hook(output=BrokenOutput()))
        self.assertIsNone(self.hook())
        self.assertIn('Deny output never reached the caller', self.receive(mid)[1]['text'])

    def test_legacy_checkpoint_keeps_recorded_read_and_allows_only_explicit_recovery(self):
        mid = self.message('Retained legacy hook body')
        self.server.call('shutdown')
        self.server.process.communicate(timeout=2)
        checkpoint = Path(self.server.state_dir) / 'correspondence.json'
        raw = json.loads(checkpoint.read_bytes())
        record, = raw['messages']
        # The legacy checkpoint retained no note or event distinguishing a
        # prior queue read from a hook that might never have surfaced.
        record.update(codex_queue_read='retrieved', state='received', delivered_at=time.time())
        message_store.atomic_write(checkpoint, message_store.encoded(raw))
        self.server.start()

        self.assertFalse(self.call('ping')['correspondence_lost'])
        restored = self.call('show', id=mid)
        self.assertEqual((restored['state'], restored['codex_queue_read']), ('received', 'retrieved'))
        self.assertNotIn('codex_hook_offer_recorded', restored)
        self.assertIsNone(self.hook())
        self.assertEqual(self.receive(mid), self.duplicate(mid))
        self.assertEqual(self.receive(mid, reread=True, session=OTHER), (1, {'error': broker.CODEX_READ_NO_MATCH}))
        self.assertIn('Retained legacy hook body', self.receive(mid, reread=True)[1]['text'])
        self.assertEqual(self.call('show', id=mid), restored)

    def test_bounded_utf8_truncation_can_reread_after_plain_receive_and_defers_next_message(self):
        body = '한글' * 1800 + 'end of the long correction'
        first = self.message(body)
        second = self.message('Next in line')
        reason = self.hook()
        self.assertLessEqual(len(reason.encode('utf-8')), codex_hook_mail.MAX_REASON_BYTES)
        self.assertIn('한글', reason)
        self.assertNotIn('end of the long correction', reason)
        self.assertIn(f'receive-codex {first} --reread]', reason)
        self.assertIn('truncated', reason)
        self.assertNotIn(second, reason)
        status, received = self.receive(first)
        self.assertEqual(status, 0)
        self.assertIn(body, received['text'])
        self.assertIs(received['marked_received'], True)
        self.assertEqual(self.receive(first), self.duplicate(first))
        self.assertEqual(self.receive(first, reread=True, session=OTHER), (1, {'error': broker.CODEX_READ_NO_MATCH}))
        status, reread = self.receive(first, reread=True)
        self.assertEqual(status, 0)
        self.assertIn(body, reread['text'])
        self.assertIs(reread['marked_received'], False)
        self.assertIn(second, self.hook())

    def test_active_pin_matches_while_closed_pin_and_removed_route_do_not(self):
        self.call('channel-bind', project='demo', name='mailbox', owner='target', description='pinned')
        valid = self.message('Pinned target', channel='mailbox')
        self.assertIn(valid, self.hook())
        dropped = self.message('Closed pinned route', channel='mailbox')
        self.call('channel-close', project='demo', name='mailbox', expect_generation=1)
        endpoint = self.message('Removed target route')
        self.call('unregister', project='demo', name='target')
        self.assertIsNone(self.hook())
        for mid in (dropped, endpoint):
            self.assertEqual(self.call('show', id=mid)['state'], 'submitted')

    def test_legacy_notice_works_and_definite_enqueue_failure_leaves_unread_pending(self):
        legacy = self.message('Already queued before this release', armed=False)
        self.assertIsNone(self.hook())
        self.assertIn('Already queued before this release', self.receive(legacy)[1]['text'])
        early = self.message('Queue rejected the attempt', submitted=False)
        result = self.call('finish', id=early, state='pending', event='push_failed', note='Queue prewrite refusal')
        self.assertEqual(result['state'], 'pending')
        self.assertNotIn('codex_queue_read', result)
        self.assertIsNone(self.hook())

    def test_early_offer_does_not_turn_queue_rejection_into_receipt(self):
        early = self.message('Hook offered before queue attempt finished', submitted=False)
        self.assertIn(early, self.hook())
        result = self.call('finish', id=early, state='pending', event='push_failed', note='Queue prewrite refusal')
        self.assertEqual(result['state'], 'pending')
        self.assertFalse(result.get('codex_queue_accepted', False))
        self.assertNotIn('codex_queue_read', result)
        self.assertIs(result['codex_hook_offer_recorded'], True)
        self.call('claim', id=early)
        self.call('codex-queue-arm', id=early)
        self.assertIsNone(self.hook())
        status, received = self.receive(early)
        self.assertEqual(status, 0)
        self.assertIn('Hook offered before queue attempt finished', received['text'])
        self.assertIs(received['marked_received'], False)
        dispatching = self.call('show', id=early)
        self.assertEqual(dispatching['state'], 'dispatching')
        self.assertNotIn('received', [e['state'] for e in dispatching['events']])
        self.call('finish', id=early, state='submitted', codex_queue_accepted=True)
        self.assertEqual(self.receive(early), self.duplicate(early))
        settled = self.call('show', id=early)
        self.assertEqual(settled['state'], 'received')
        self.assertEqual([e['note'] for e in settled['events'] if e['state'] == 'received'], [None])

    def test_legacy_hook_only_gets_stop_and_cannot_consume_queue_read(self):
        mid = self.message('Still readable with an old hook')
        deadline = time.monotonic() + 0.25
        for op in ('codex-pretool-claim', 'codex-pretool-commit'):
            with self.assertRaisesRegex(ValueError, '^Unknown operation$'):
                self.call(op, session=THREAD, deadline=deadline, id=OTHER, digest='0' * 64)
        self.call('codex-stop', project='demo', name='target', reason='Legacy stop still works')
        stop = self.call('codex-stop-claim', session=THREAD, deadline=time.monotonic() + 0.25)
        self.assertEqual(stop['reason'], 'Legacy stop still works')
        self.assertIs(self.call('codex-stop-finish', session=THREAD, id=stop['id']), True)
        self.assertIn('Still readable with an old hook', self.receive(mid)[1]['text'])


class HookBudgetTests(unittest.TestCase):
    def test_old_broker_uses_existing_stop_contract(self):
        with patch.object(broker, 'request', side_effect=[ValueError('Unknown operation'), None]) as request:
            self.assertIsNone(codex_stop_hook.claim('fake.sock', THREAD, time.monotonic() + 0.25))
            self.assertEqual([call.args[1]['op'] for call in request.call_args_list],
                             [codex_hook_mail.CLAIM_OP, 'codex-stop-claim'])

    def test_over_budget_preview_records_no_offer(self):
        source = io.BytesIO(json.dumps({'session_id': THREAD, 'hook_event_name': 'PreToolUse'}).encode())
        output = io.StringIO()
        proposal = {'kind': 'mail', 'id': OTHER, 'reason': codex_hook_mail.PREFIX + '\n\nbody'}
        with patch.object(broker, 'request', return_value=proposal) as request, \
                patch.object(codex_stop_hook.time, 'monotonic', side_effect=[10, 11]):
            codex_stop_hook.run('fake.sock', source, output)
        request.assert_called_once()
        self.assertEqual(output.getvalue(), '')
