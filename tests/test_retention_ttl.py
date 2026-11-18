"""TTL contract on synthetic routes/checkpoints only; never opens provider apps."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
import broker
import message_store
import retention
import route_store
from support import RunningBroker

CLOCK = 2_000_000_000


class RetentionTTLTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(broker.time, 'time', return_value=CLOCK).start()
        patch.object(broker, 'now', side_effect=lambda: datetime.fromtimestamp(self.clock.return_value, timezone.utc).isoformat()).start()
        self.addCleanup(patch.stopall)
        self.state = broker.State(3, retention_ttl=True)
        self.register(self.state)

    @staticmethod
    def register(state):
        for name in ('alpha', 'beta'):
            state.action(dict(op='register', project='demo', name=name, cwd='/tmp'))

    def call(self, op, **fields):
        return self.state.action(dict(op=op, **fields))

    def send(self, key='q', **fields):
        return self.call('send', project='demo', sender='alpha', recipient='beta',
                         body='sensitive payload', key=key, **fields)

    def complete(self, message):
        self.call('resolve', id=message['id'], state='done', note='Completed explicitly')

    def sweep(self, elapsed):
        self.clock.return_value = CLOCK + elapsed
        return self.call('retention-sweep')

    def test_terminal_body_ttl_still_uses_completion_not_creation(self):
        message = self.send()
        self.clock.return_value += retention.BODY_TTL * 3
        self.call('ack', id=message['id'])
        self.complete(message)
        self.assertFalse(self.sweep(retention.BODY_TTL * 4 - 1)['changed'])
        self.assertTrue(self.sweep(retention.BODY_TTL * 4)['changed'])
        compact = self.call('show', id=message['id'])
        self.assertTrue(compact['body_expired'])
        self.assertEqual(compact['body'], '')
        self.assertEqual(len(compact['body_sha256']), 64)
        self.assertEqual(self.send()['id'], message['id'])
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.call('send', project='demo', sender='alpha', recipient='beta', body='other', key='q')

    def test_old_submitted_and_received_are_removed_regardless_of_intent(self):
        for state, fields in (('submitted', {}), ('received', {}), ('submitted', {'one_way': True})):
            with self.subTest(state=state, fields=fields):
                self.state = broker.State(3, retention_ttl=True)
                self.register(self.state)
                self.clock.return_value = CLOCK
                message = self.send(**fields)
                self.call('resolve', id=message['id'], state='submitted', note='Synthetic acceptance')
                if state == 'received':
                    self.clock.return_value = CLOCK + retention.BODY_TTL - 1
                    self.call('ack', id=message['id'])
                self.assertFalse(self.sweep(retention.BODY_TTL - 1)['changed'])
                self.assertTrue(self.sweep(retention.BODY_TTL)['changed'])
                self.assertNotIn(message['id'], self.state.messages)
                self.assertNotIn(message['id'], self.state.compact_messages)
                self.assertNotIn('q', self.state.keys)
                with self.assertRaisesRegex(ValueError, 'Unknown message'):
                    self.call('reply', id=message['id'], body='late answer', key='late')

    def test_age_removal_ignores_optional_reply_dependencies_but_keeps_live_wait(self):
        parent = self.send('parent')
        reply = self.call('reply', id=parent['id'], body='unconsumed answer', key='answer')
        self.call('ack', id=reply['id'])
        self.state.waiters[parent['id']] = 1
        self.sweep(retention.BODY_TTL)
        self.assertIn(reply['id'], self.state.messages)
        del self.state.waiters[parent['id']]
        self.sweep(retention.BODY_TTL)
        self.assertNotIn(reply['id'], self.state.messages)
        self.assertIn(parent['id'], self.state.messages)

    def test_live_wait_and_provider_source_are_preserved_until_released(self):
        source = self.send('source')
        self.call('resolve', id=source['id'], state='submitted', note='Synthetic source')
        control = self.send('control')
        self.state.messages[control['id']]['provider_retry'] = {'status': 'reserved', 'source_message_id': source['id']}
        self.sweep(retention.BODY_TTL)
        self.assertIn(source['id'], self.state.messages)
        self.state.messages[control['id']]['provider_retry']['status'] = 'recovered'
        self.state.waiters[source['id']] = 1
        self.sweep(retention.BODY_TTL)
        self.assertIn(source['id'], self.state.messages)
        del self.state.waiters[source['id']]
        self.sweep(retention.BODY_TTL)
        self.assertNotIn(source['id'], self.state.messages)
        self.assertIn(control['id'], self.state.messages)

    def test_age_removal_frees_capacity_without_compact_quota(self):
        terminal = self.send('terminal')
        self.complete(terminal)
        for key in ('submitted', 'received'):
            message = self.send(key)
            self.call('resolve', id=message['id'], state='submitted', note='Synthetic acceptance')
            if key == 'received':
                self.call('ack', id=message['id'])
        self.clock.return_value += retention.BODY_TTL
        with patch.object(retention, 'MAX_COMPACT', 0):
            plan = self.call('retention-plan')
            self.assertEqual((plan['would_remove'], plan['would_compact'], plan['free_slots_after']), (2, 0, 2))
            self.assertFalse(plan['admission_blocked'])
            admitted = self.send('new')
        self.assertEqual(set(self.state.messages), {terminal['id'], admitted['id']})
        self.assertEqual(set(self.state.keys), {'terminal', 'new'})
        self.assertEqual(self.state.compact_messages, {})

    def test_stale_removal_plan_rechecks_state_age_wait_and_episode(self):
        message = self.send()
        self.call('ack', id=message['id'])
        self.clock.return_value += retention.BODY_TTL
        planned = self.state.retention_plan()
        self.assertEqual(planned['remove'], [message['id']])
        for change in ('state', 'created', 'wait', 'provider'):
            with self.subTest(change=change):
                row = self.state.messages[message['id']]
                original = deepcopy(row)
                if change == 'state':
                    row['state'] = 'in_progress'
                elif change == 'created':
                    row['created'] = broker.now()
                elif change == 'wait':
                    self.state.waiters[row['id']] = 1
                else:
                    row['provider_retry'] = {'status': 'uncertain'}
                self.assertFalse(self.state.sweep_retention(planned=planned))
                self.assertIn(row['id'], self.state.messages)
                self.state.messages[row['id']] = original
                self.state.waiters.clear()

    def test_no_oldest_eviction_before_ttl_and_clear_capacity_reason(self):
        for key in ('a', 'b', 'c'):
            self.complete(self.send(key))
        before = deepcopy(self.state.messages)
        with self.assertRaisesRegex(ValueError, 'TTL or protected work'):
            self.send('d')
        self.assertEqual(self.state.messages, before)
        self.clock.return_value += retention.BODY_TTL
        self.send('d')
        self.assertEqual(len(self.state.messages), 1)
        self.assertEqual(len(self.state.compact_messages), 3)

    def test_wait_and_unread_reply_protection_then_explicit_observation(self):
        parent = self.send()
        reply = self.call('reply', id=parent['id'], body='answer', key='a')
        self.sweep(retention.BODY_TTL * 2)
        self.assertEqual(len(self.state.messages), 2)
        self.complete(reply)
        self.state.waiters[parent['id']] = 1
        self.sweep(retention.BODY_TTL * 3)
        self.assertEqual(len(self.state.messages), 2)
        del self.state.waiters[parent['id']]
        self.sweep(retention.BODY_TTL * 3)
        self.assertEqual(len(self.state.compact_messages), 2)

    def test_pending_dispatching_uncertain_and_provider_episode_protected(self):
        for state in ('pending', 'dispatching', 'uncertain'):
            with self.subTest(state=state):
                self.state = broker.State(3, retention_ttl=True)
                self.register(self.state)
                message = self.send()
                self.state.messages[message['id']].update(state=state, completed_at=CLOCK)
                self.sweep(retention.BODY_TTL * 3)
                self.assertIn(message['id'], self.state.messages)
        self.state.messages[message['id']].update(state='done', provider_retry={'status': 'waiting'})
        self.sweep(retention.BODY_TTL * 4)
        self.assertIn(message['id'], self.state.messages)

    def test_received_deadline_request_uses_created_age_and_loses_late_route(self):
        message = self.send(reply_deadline_seconds=retention.LOOKUP_TTL)
        self.call('ack', id=message['id'])
        self.sweep(retention.BODY_TTL - 1)
        self.assertEqual(self.call('show', id=message['id'])['request_status'], 'open')
        self.sweep(retention.BODY_TTL)
        with self.assertRaisesRegex(ValueError, 'Unknown message'):
            self.call('show', id=message['id'])
        with self.assertRaisesRegex(ValueError, 'Unknown message'):
            self.call('reply', id=message['id'], body='late answer', key='late')

    def test_expired_pending_is_durable_and_claim_is_refused(self):
        message = self.send(reply_deadline_seconds=10)
        self.sweep(retention.BODY_TTL * 8)
        self.assertIn(message['id'], self.state.messages)
        with self.assertRaisesRegex(ValueError, 'deadline expired'):
            self.call('claim', id=message['id'])
        self.assertEqual(self.state.messages[message['id']]['state'], 'pending')

    def test_timely_answer_stays_answered_with_late_followup_explicit(self):
        message = self.send(reply_deadline_seconds=60)
        self.clock.return_value += 10
        self.call('reply', id=message['id'], body='timely', key='r1')
        self.clock.return_value += 60
        shown = self.call('show', id=message['id'])
        self.assertEqual(shown['request_status'], 'answered')
        self.assertFalse(shown['late_reply'])
        self.call('reply', id=message['id'], body='later', key='r2')
        self.assertTrue(self.call('show', id=message['id'])['late_reply'])

    def test_compact_lookup_own_ttl_and_protected_capacity(self):
        message = self.send()
        self.complete(message)
        self.sweep(retention.BODY_TTL)
        self.sweep(retention.BODY_TTL + retention.LOOKUP_TTL - 1)
        self.assertEqual(self.send()['id'], message['id'])
        self.sweep(retention.BODY_TTL + retention.LOOKUP_TTL)
        self.assertNotIn(message['id'], self.state.compact_messages)
        self.assertNotIn('q', self.state.keys)
        self.assertNotEqual(self.send()['id'], message['id'])

    def test_legacy_timestamp_adoption_and_correlated_reply_bound(self):
        message = self.send()
        self.complete(message)
        del self.state.messages[message['id']]['completed_at']
        self.sweep(retention.BODY_TTL * 10)
        self.assertIn(message['id'], self.state.messages)
        self.assertEqual(self.state.messages[message['id']]['retention_adopted_at'], CLOCK + retention.BODY_TTL * 10)
        self.sweep(retention.BODY_TTL * 11)
        self.assertIn(message['id'], self.state.compact_messages)
        self.clock.return_value = CLOCK
        parent = self.send('legacy')
        reply = self.call('reply', id=parent['id'], body='answer', key='legacy-reply')
        self.complete(reply)
        del self.state.messages[parent['id']]['completed_at']
        self.sweep(retention.BODY_TTL)
        self.assertIn(parent['id'], self.state.compact_messages)

    def test_read_only_plan_reports_counts_and_blocked_activation(self):
        for key in ('a', 'b', 'c'):
            self.send(key)
        before = deepcopy(self.state.messages)
        planned = self.call('retention-plan')
        self.assertTrue(planned['admission_blocked'])
        self.assertEqual(planned['free_slots_after'], 0)
        self.assertTrue(planned['live_waiters_visible'])
        self.assertEqual(self.state.messages, before)
        output = json.dumps(planned)
        self.assertNotIn('sensitive', output)
        self.assertNotIn(next(iter(self.state.messages)), output)

    def test_in_progress_request_remains_full_after_deadline(self):
        message = self.send(reply_deadline_seconds=1)
        self.call('ack', id=message['id'])
        self.call('progress', id=message['id'], note='Still working')
        self.sweep(retention.BODY_TTL * 10)
        shown = self.call('show', id=message['id'])
        self.assertEqual(shown['request_status'], 'expired')
        self.assertEqual(shown['state'], 'in_progress')
        self.assertEqual(shown['body'], 'sensitive payload')
        self.assertNotIn(message['id'], self.state.compact_messages)

    def test_oversize_compact_route_is_skipped_without_mutation(self):
        message = self.send()
        self.complete(message)
        self.state.messages[message['id']]['route']['cwd'] = '/' + 'x' * retention.MAX_COMPACT_BYTES
        self.sweep(retention.BODY_TTL)
        self.assertIn(message['id'], self.state.messages)
        counts = self.call('retention-plan')
        self.assertEqual(counts['protected']['compact_record_too_large'], 1)
        self.send('other')

    def test_oneway_text_does_not_instruct_reply_default_text_stable(self):
        default = self.send()
        one_way = self.send('one', one_way=True)
        self.assertIn('Answer through the bridge', bridge.peer_text(default, '/tmp/synthetic.sock'))
        self.assertNotIn('Answer through the bridge', bridge.peer_text(one_way, '/tmp/synthetic.sock'))
        self.assertIn('no reply requested', bridge.peer_text(one_way, '/tmp/synthetic.sock'))

    def test_compact_parent_wait_observes_live_late_child(self):
        message = self.send(reply_deadline_seconds=1)
        self.clock.return_value = CLOCK + 2
        self.complete(message)
        self.sweep(retention.BODY_TTL + 2)
        reply = self.call('reply', id=message['id'], body='late', key='late')
        async def run():
            reader = asyncio.StreamReader()
            return await broker.wait_answer(self.state, dict(id=message['id'], timeout=.1), reader)
        result = asyncio.run(run())
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['replies'][0]['id'], reply['id'])
        self.assertTrue(result['body_expired'])


class RetentionCheckpointTests(unittest.TestCase):
    register = staticmethod(RetentionTTLTests.register)
    call = RetentionTTLTests.call
    send = RetentionTTLTests.send
    complete = RetentionTTLTests.complete
    sweep = RetentionTTLTests.sweep

    def setUp(self):
        RetentionTTLTests.setUp(self)
        self.root = tempfile.TemporaryDirectory(prefix='ab-ttl-synthetic-')
        self.addCleanup(self.root.cleanup)
        self.routes = route_store.RouteStore(Path(self.root.name) / 'state')
        self.addCleanup(self.routes.close)
        self.state = broker.State(3, self.routes, retention_ttl=True)
        self.register(self.state)

    def test_compact_save_atomic_dedup_digest_restore_and_disable_safe(self):
        message = self.send()
        self.complete(message)
        self.sweep(retention.BODY_TTL)
        raw = self.state.correspondence.path.read_text()
        self.assertNotIn('sensitive payload', raw)
        saved = json.loads(raw)
        self.assertEqual(saved['version'], 2)
        self.assertEqual(set(saved['keys'][0][1]), {'fingerprint'})
        self.state = broker.State(3, self.routes, retention_ttl=False)
        self.assertEqual(self.send()['id'], message['id'])
        self.assertTrue(self.call('show', id=message['id'])['body_expired'])
        self.assertFalse(self.call('retention-sweep')['changed'])

    def test_age_removal_is_atomic_then_noop_and_key_can_be_reused_explicitly(self):
        message = self.send()
        self.call('ack', id=message['id'])
        before = self.state.correspondence.path.read_bytes()
        self.clock.return_value += retention.BODY_TTL
        with patch.object(self.state.correspondence, 'save', side_effect=OSError('synthetic failed save')):
            with self.assertRaises(OSError):
                self.call('retention-sweep')
        self.assertIn(message['id'], self.state.messages)
        self.assertIn('q', self.state.keys)
        self.assertEqual(self.state.correspondence.path.read_bytes(), before)
        self.assertTrue(self.call('retention-sweep')['changed'])
        raw = json.loads(self.state.correspondence.path.read_text())
        self.assertEqual((raw['messages'], raw['keys'], raw['compact']), ([], [], []))
        with patch.object(self.state.correspondence, 'save') as save:
            self.assertFalse(self.call('retention-sweep')['changed'])
            save.assert_not_called()
        self.assertNotEqual(self.send()['id'], message['id'])

    def test_age_read_only_restore_and_prepare_have_no_false_loss(self):
        message = self.send()
        self.call('ack', id=message['id'])
        self.clock.return_value += retention.BODY_TTL
        before = self.state.correspondence.path.read_bytes()
        counts = self.state.correspondence.retention_dry_run()
        self.assertEqual(counts['would_remove'], 1)
        self.assertEqual(counts['free_slots_after'], 3)
        self.assertEqual(self.state.correspondence.path.read_bytes(), before)
        self.state = broker.State(3, self.routes, retention_prepare=True)
        self.assertIn(message['id'], self.state.messages)
        self.assertFalse(self.call('retention-sweep')['changed'])
        with patch.object(message_store.MessageStore, 'record_loss', side_effect=AssertionError('Not accidental loss')):
            self.state = broker.State(3, self.routes, retention_ttl=True)
        self.assertNotIn(message['id'], self.state.messages)
        self.assertFalse(self.call('ping')['correspondence_lost'])
        self.assertFalse((self.routes.directory / 'correspondence-lost.json').exists())

    def test_durable_late_reply_preserves_compaction_anchor_and_pinned_route(self):
        message = self.send(reply_deadline_seconds=1)
        self.call('ack', id=message['id'])
        original_route = deepcopy(message['return_route'])
        # A receipt produced by the earlier deadline policy remains readable;
        # the new active-row age rule must not rewrite existing compact mail.
        self.clock.return_value = CLOCK + retention.BODY_TTL + 1
        self.state.compact_messages[message['id']] = retention.compact_message(
            self.state.messages.pop(message['id']), self.clock.return_value, CLOCK + 1)
        intent, mid = self.state.keys['q']
        self.state.keys['q'] = ({'fingerprint': retention.fingerprint(intent)}, mid)
        self.state.correspondence.save(self.state)
        compact = self.call('show', id=message['id'])
        anchor = compact['body_ttl_anchor_at']
        self.assertEqual(anchor, CLOCK + 1)
        self.call('register', project='demo', name='alpha', cwd='/tmp', session=str(uuid.uuid4()))
        self.clock.return_value += 10
        reply = self.call('reply', id=message['id'], body='late persisted answer', key='late-durable')
        self.assertEqual(reply['route'], original_route)
        self.assertGreater(self.state.compact_messages[message['id']]['completed_at'], compact['compacted_at'])
        self.state = broker.State(3, self.routes, retention_ttl=False)
        restored = self.call('show', id=message['id'])
        self.assertEqual(restored['body_ttl_anchor_at'], anchor)
        self.assertEqual(restored['request_status'], 'expired')
        self.assertEqual(restored['state'], 'answered')
        self.assertTrue(restored['late_reply'])
        self.assertTrue(restored['body_expired'])
        self.assertEqual(self.call('show', id=reply['id'])['route'], original_route)
        self.assertEqual(self.call('show', id=reply['id'])['body'], 'late persisted answer')

    def test_compaction_anchor_is_required_bounded_and_not_allowed_on_full_rows(self):
        message = self.send()
        self.complete(message)
        raw = json.loads(self.state.correspondence.path.read_text())
        raw['messages'][0]['body_ttl_anchor_at'] = CLOCK
        with self.assertRaisesRegex(ValueError, 'only to compact'):
            self.state.correspondence.decode(raw)
        self.sweep(retention.BODY_TTL)
        raw = json.loads(self.state.correspondence.path.read_text())
        for value in (None, raw['compact'][0]['compacted_at']):
            raw['compact'][0]['body_ttl_anchor_at'] = value
            with self.assertRaisesRegex(ValueError, 'safe body TTL'):
                self.state.correspondence.decode(raw)

    def test_failed_compaction_save_leaves_memory_and_disk_unchanged(self):
        message = self.send()
        self.complete(message)
        before = self.state.correspondence.path.read_bytes()
        self.clock.return_value += retention.BODY_TTL
        with patch.object(self.state.correspondence, 'save', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                self.call('retention-sweep')
        self.assertIn(message['id'], self.state.messages)
        self.assertEqual(self.state.compact_messages, {})
        self.assertEqual(self.state.correspondence.path.read_bytes(), before)

    def test_noop_sweep_skips_write_but_startup_reconciles_checkpoint(self):
        self.send()
        with patch.object(self.state.correspondence, 'save') as save:
            self.assertFalse(self.sweep(5)['changed'])
            save.assert_not_called()
        with patch.object(message_store.MessageStore, 'save') as save:
            broker.State(3, self.routes, retention_ttl=True)
            save.assert_called_once()

    def test_prepare_warms_unknown_legacy_without_deletion_then_strict_activates(self):
        message = self.send()
        self.complete(message)
        del self.state.messages[message['id']]['completed_at']
        self.state.correspondence.save(self.state)
        self.clock.return_value += retention.BODY_TTL * 10
        self.state = broker.State(3, self.routes, retention_prepare=True)
        shown = self.call('show', id=message['id'])
        self.assertEqual(shown['body'], 'sensitive payload')
        self.assertEqual(shown['retention_adopted_at'], self.clock.return_value)
        self.assertEqual(self.call('retention-plan')['retention_mode'], 'prepare')
        self.clock.return_value += retention.BODY_TTL
        self.assertFalse(self.call('retention-sweep')['changed'])
        self.assertEqual(self.call('retention-plan')['would_compact'], 1)
        self.assertIn(message['id'], self.state.messages)
        self.state = broker.State(3, self.routes, retention_ttl=True)
        self.assertTrue(self.call('show', id=message['id'])['body_expired'])

    def test_prepare_never_changes_claim_deadline_or_active_reply(self):
        parent = self.send(reply_deadline_seconds=60)
        reply = self.call('reply', id=parent['id'], body='unobserved answer', key='reply')
        pending = self.send('pending')
        self.call('claim', id=pending['id'])
        self.state = broker.State(3, self.routes, retention_prepare=True)
        self.clock.return_value += retention.BODY_TTL * 10
        self.call('retention-sweep')
        self.assertEqual(self.call('show', id=parent['id'])['reply_deadline_at'], parent['reply_deadline_at'])
        self.assertEqual(self.call('show', id=reply['id'])['body'], 'unobserved answer')
        self.assertEqual(self.call('show', id=pending['id'])['state'], 'uncertain')
        self.assertEqual(self.state.compact_messages, {})

    def test_gated_send_is_atomic_and_persists_intent(self):
        fields = dict(project='demo', sender='alpha', recipient='beta', body='synthetic', key='gated', one_way=True)
        before = self.state.correspondence.path.read_bytes()
        with patch.object(self.state.correspondence, 'save', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.call('send-with-retention', **fields)
        self.assertEqual(self.state.messages, {})
        self.assertEqual(self.state.correspondence.path.read_bytes(), before)
        message = self.call('send-with-retention', **fields)
        self.assertTrue(message['one_way'])
        self.assertEqual(self.call('send-with-retention', **fields)['id'], message['id'])
        self.assertEqual(json.loads(self.state.correspondence.path.read_text())['version'], 2)

    def test_explicit_intent_marks_v2_before_first_compaction(self):
        for fields in ({'one_way': True}, {'reply_deadline_seconds': 30}):
            self.send(str(len(self.state.messages)), **fields)
            saved = json.loads(self.state.correspondence.path.read_text())
            self.assertEqual(saved['version'], 2)
            self.assertEqual(saved['compact'], [])
            self.state.correspondence.decode(saved)
            saved['version'] = 1
            with self.assertRaisesRegex(ValueError, 'version 2'):
                self.state.correspondence.decode(saved)

    def test_malformed_compact_active_state_rejected_without_replay(self):
        message = self.send()
        self.complete(message)
        self.sweep(retention.BODY_TTL)
        saved = json.loads(self.state.correspondence.path.read_text())
        for state in ('pending', 'dispatching', 'uncertain', 'in_progress'):
            saved['compact'][0]['state'] = state
            with self.assertRaisesRegex(ValueError, 'Unsafe compact message state'):
                self.state.correspondence.decode(saved)

    def test_noop_tick_scans_once_and_does_not_clone_maps(self):
        self.send()
        with patch.object(self.state, 'retention_plan', wraps=self.state.retention_plan) as planner, patch.object(broker, 'deepcopy', side_effect=AssertionError('No idle deep copy')):
            self.assertFalse(self.call('retention-sweep')['changed'])
        self.assertEqual(planner.call_count, 1)

    def test_offline_dry_run_is_read_only_counts_only_shared_plan(self):
        message = self.send()
        self.complete(message)
        self.clock.return_value += retention.BODY_TTL
        before = {p.name: p.read_bytes() for p in self.routes.directory.iterdir() if p.is_file()}
        planned = self.state.correspondence.retention_dry_run()
        self.assertEqual(planned['would_compact'], self.state.retention_plan()['counts']['would_compact'])
        self.assertFalse(planned['live_waiters_visible'])
        self.assertTrue(planned['counts_are_upper_bound'])
        self.assertNotIn(message['id'], json.dumps(planned))
        after = {p.name: p.read_bytes() for p in self.routes.directory.iterdir() if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.state.messages[message['id']]['body'], 'sensitive payload')

    def test_offline_invalid_checkpoint_refuses_without_quarantine(self):
        path = self.state.correspondence.path
        path.write_text('{invalid')
        before = set(self.routes.directory.iterdir())
        with self.assertRaises(ValueError):
            self.state.correspondence.retention_dry_run()
        self.assertEqual(path.read_text(), '{invalid')
        self.assertEqual(set(self.routes.directory.iterdir()), before)


class RetentionCLITests(unittest.TestCase):
    def test_new_intent_refuses_old_broker_before_send(self):
        with patch.object(bridge.broker, 'request', side_effect=ValueError('Unknown operation')) as request, patch.object(bridge.sys, 'stdin', io.StringIO('synthetic')), redirect_stdout(io.StringIO()) as output:
            code = bridge.main(['send', '--project', 'demo', '--from', 'alpha', '--to', 'beta',
                                '--one-way', '--key', 'k', '--file', '-'])
        self.assertEqual(code, 1)
        self.assertIn('Unknown operation', output.getvalue())
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1]['op'], 'send-with-retention')
        self.assertTrue(request.call_args.args[1]['one_way'])

    @staticmethod
    def old_created_broker(mode='--retention-ttl'):
        runner = RunningBroker(max_messages=1)
        runner.usage.append(mode)
        start = runner.start
        popen = subprocess.Popen
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        code = "import broker,bridge; broker.now=lambda: " + repr(old) + "; raise SystemExit(bridge.main())"
        def start_with_old_created():
            def launch(arguments, **kwargs):
                return popen([arguments[0], '-c', code, *arguments[2:]], **kwargs)
            with patch.object(subprocess, 'Popen', side_effect=launch):
                start()
        runner.start = start_with_old_created
        return runner

    @staticmethod
    def cli(runner, *arguments, body=None):
        return subprocess.run([sys.executable, bridge.__file__, '--socket', runner.path, *arguments],
                              input=body, capture_output=True, text=True, timeout=5)

    def test_real_cli_age_preflight_and_pre_admission_without_compaction(self):
        with self.old_created_broker() as runner:
            runner.register_pair()
            old = self.cli(runner, 'send', '--project', 'demo', '--from', 'alpha', '--to', 'beta', '--key', 'old', '--file', '-', body='synthetic old body')
            self.assertEqual(old.returncode, 0, old.stdout + old.stderr)
            mid = json.loads(old.stdout)['id']
            self.assertEqual(self.cli(runner, 'ack', mid).returncode, 0)
            checkpoint = Path(runner.state_dir) / 'correspondence.json'
            before = checkpoint.read_bytes()
            for flags in (('--live',), ('--dry-run', '--state-dir', runner.state_dir, '--max-messages', '1')):
                result = self.cli(runner, 'retention', *flags)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                counts = json.loads(result.stdout)
                self.assertEqual((counts['would_remove'], counts['free_slots_after']), (1, 1))
                self.assertEqual(checkpoint.read_bytes(), before)
            self.assertEqual(self.cli(runner, 'show', mid).returncode, 0)
            admitted = self.cli(runner, 'send', '--project', 'demo', '--from', 'alpha', '--to', 'beta', '--key', 'new', '--file', '-', body='new pending body')
            self.assertEqual(admitted.returncode, 0, admitted.stdout + admitted.stderr)
            self.assertEqual(self.cli(runner, 'show', mid).returncode, 1)
            late = self.cli(runner, 'reply', mid, '--key', 'late', '--file', '-', body='late answer')
            self.assertEqual(late.returncode, 1)
            self.assertIn('Unknown message', late.stdout)
            saved = json.loads(checkpoint.read_text())
            self.assertEqual([m['id'] for m in saved['messages']], [json.loads(admitted.stdout)['id']])
            self.assertEqual([key for key, _, _ in saved['keys']], ['new'])
            self.assertEqual(saved['compact'], [])

    def test_real_cli_prepare_then_ttl_restore_removes_without_loss(self):
        with self.old_created_broker('--retention-prepare') as runner:
            runner.register_pair()
            result = self.cli(runner, 'send', '--project', 'demo', '--from', 'alpha', '--to', 'beta', '--key', 'old', '--file', '-', body='old prepared body')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            mid = json.loads(result.stdout)['id']
            self.assertEqual(self.cli(runner, 'ack', mid).returncode, 0)
            runner.call('retention-sweep')
            self.assertEqual(self.cli(runner, 'show', mid).returncode, 0)
            runner.usage[runner.usage.index('--retention-prepare')] = '--retention-ttl'
            runner.restart()
            self.assertEqual(self.cli(runner, 'show', mid).returncode, 1)
            ping = runner.call('ping')
            self.assertEqual(ping['messages'], 0)
            self.assertFalse(ping['correspondence_lost'])
            self.assertFalse((Path(runner.state_dir) / 'correspondence-lost.json').exists())

    def test_live_plan_only_uses_read_only_op(self):
        with patch.object(bridge.broker, 'request', return_value={'dry_run': True}) as request, redirect_stdout(io.StringIO()):
            self.assertEqual(bridge.main(['retention', '--live']), 0)
        self.assertEqual(request.call_args.args[1], {'op': 'retention-plan'})


if __name__ == '__main__':
    unittest.main()
