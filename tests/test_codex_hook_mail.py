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
import codex_queue
import codex_stop_hook
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

    def hook(self, session=THREAD):
        source = io.BytesIO(json.dumps({'hook_event_name': 'PreToolUse', 'session_id': session}).encode())
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

    def test_ordered_messages_share_one_denial_and_later_plain_queue_reads_are_empty(self):
        first = self.message('Peer correction one')
        second = self.message('Peer correction two')
        self.assertIsNone(self.hook(OTHER))
        self.assertEqual(self.receive(first, session=OTHER), (1, {'error': broker.CODEX_READ_NO_MATCH}))
        reason = self.hook()
        self.assertIn('may change your current task', reason)
        self.assertIn('decide how to proceed within existing user authorization', reason)
        self.assertIn(PEER_SCOPE, reason)
        self.assertIn('peer -> target', reason)
        self.assertLess(reason.index(first), reason.index(second))
        self.assertLess(reason.index('Peer correction one'), reason.index('Peer correction two'))
        self.assertNotIn('End the turn', reason)
        self.assertIsNone(self.hook())
        for mid in (first, second):
            shown = self.call('show', id=mid)
            self.assertEqual((shown['state'], shown['note']), ('received', broker.CODEX_READ_AT_TOOL_CALL))
            self.assertEqual(self.receive(mid), self.duplicate(mid))

    def test_stop_wins_before_proposal_and_while_mail_waits_to_commit(self):
        first = self.message('Peer correction')
        self.call('codex-stop', project='demo', name='target', reason='Take a break')
        self.assertIn('Take a break', self.hook())
        self.assertEqual(self.call('show', id=first)['state'], 'submitted')
        request = broker.request

        def arrive_during_preview(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == 'codex-pretool-claim':
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

    def test_queue_can_win_between_hook_preview_and_atomic_commit(self):
        first = self.message('Already read by queue')
        second = self.message('Still available')
        request = broker.request

        def read_during_preview(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == 'codex-pretool-claim':
                self.assertIn(first, result['reason'])
                self.assertIn(second, result['reason'])
                self.assertIn('Already read by queue', self.receive(first)[1]['text'])
            return result

        with patch.object(broker, 'request', side_effect=read_during_preview):
            self.assertIsNone(self.hook())
        self.assertEqual(self.call('show', id=second)['state'], 'submitted')
        self.assertEqual(self.call('show', id=first)['note'], broker.CODEX_READ_FROM_QUEUE)
        self.assertNotIn(first, self.hook())
        self.assertEqual(self.receive(second), self.duplicate(second))

    def test_oversize_preview_cannot_consume_body(self):
        first = self.message('First body remains queued')
        request = broker.request

        def corrupt(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == 'codex-pretool-claim':
                return {**result, 'reason': result['reason'] + 'x' * codex_hook_mail.MAX_REASON_BYTES}
            self.fail('An invalid preview must never commit')

        with patch.object(broker, 'request', side_effect=corrupt):
            self.assertIsNone(self.hook())
        self.assertEqual(self.call('show', id=first)['state'], 'submitted')
        self.assertIn('First body remains queued', self.receive(first)[1]['text'])

    def test_preview_digest_mismatch_cannot_confirm_a_different_body(self):
        first = self.message('Original body stays available')
        request = broker.request

        def corrupt(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == 'codex-pretool-claim':
                return {**result, 'reason': result['reason'].replace('Original body', 'Altered body')}
            return result

        with patch.object(broker, 'request', side_effect=corrupt):
            self.assertIsNone(self.hook())
        self.assertEqual(self.call('show', id=first)['state'], 'submitted')
        self.assertIn('Original body stays available', self.receive(first)[1]['text'])

    def test_checkpoint_survives_lost_confirmation_and_allows_explicit_recovery(self):
        first = self.message('Confirmation lost after durability', submitted=False)
        request = broker.request

        def lose(path, args, **kwargs):
            result = request(path, args, **kwargs)
            if args['op'] == 'codex-pretool-commit':
                self.assertEqual(result['kind'], 'mail')
                raise OSError('lost confirmation')
            return result

        with patch.object(broker, 'request', side_effect=lose):
            self.assertIsNone(self.hook())
        self.assertEqual(self.call('show', id=first)['state'], 'dispatching')
        self.server.restart()
        durable = self.call('show', id=first)
        self.assertEqual(durable['state'], 'received')
        self.assertFalse(durable.get('codex_queue_accepted', False))
        self.assertEqual(self.receive(first), self.duplicate(first))
        self.assertIn('Confirmation lost after durability', self.receive(first, reread=True)[1]['text'])
        with self.assertRaisesRegex(ValueError, 'cannot be redelivered'):
            self.call('resolve', id=first, state='pending', note='inspect')

    def test_bounded_utf8_truncation_retains_reread_and_defers_next_message(self):
        body = '한글' * 1800 + 'end of the long correction'
        first = self.message(body)
        second = self.message('Next in line')
        reason = self.hook()
        self.assertLessEqual(len(reason.encode('utf-8')), codex_hook_mail.MAX_REASON_BYTES)
        self.assertIn('한글', reason)
        self.assertNotIn('end of the long correction', reason)
        self.assertIn(f'receive-codex {first} --reread', reason)
        self.assertIn('truncated', reason)
        self.assertNotIn(second, reason)
        self.assertEqual(self.receive(first), self.duplicate(first))
        self.assertIn(body, self.receive(first, reread=True)[1]['text'])
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

    def test_confirmed_early_body_is_received_without_claiming_queue_acceptance(self):
        early = self.message('Hook retrieved before queue attempt finished', submitted=False)
        self.assertIn(early, self.hook())
        self.assertEqual(self.receive(early), self.duplicate(early))
        result = self.call('finish', id=early, state='pending', event='push_failed', note='Queue prewrite refusal')
        self.assertEqual(result['state'], 'received')
        self.assertFalse(result.get('codex_queue_accepted', False))
        self.assertIn('continue without replaying the correspondence', codex_queue.notice(early))


class HookBudgetTests(unittest.TestCase):
    def test_old_broker_uses_existing_stop_contract(self):
        with patch.object(broker, 'request', side_effect=[ValueError('Unknown operation'), None]) as request:
            self.assertIsNone(codex_stop_hook.claim('fake.sock', THREAD, time.monotonic() + 0.25))
            self.assertEqual([call.args[1]['op'] for call in request.call_args_list],
                             ['codex-pretool-claim', 'codex-stop-claim'])

    def test_over_budget_preview_is_not_committed(self):
        source = io.BytesIO(json.dumps({'session_id': THREAD, 'hook_event_name': 'PreToolUse'}).encode())
        output = io.StringIO()
        proposal = {'kind': 'mail', 'id': OTHER, 'reason': codex_hook_mail.PREFIX + '\n\nbody'}
        with patch.object(broker, 'request', return_value=proposal) as request, \
                patch.object(codex_stop_hook.time, 'monotonic', side_effect=[10, 11]):
            codex_stop_hook.run('fake.sock', source, output)
        request.assert_called_once()
        self.assertEqual(output.getvalue(), '')
