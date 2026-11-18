"""TTL contract on synthetic routes/checkpoints only; never opens provider apps."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
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

CLOCK = 2_000_000_000


class RetentionTTLTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(broker.time, 'time', return_value=CLOCK).start()
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

    def test_ttl_is_from_completion_not_creation_and_default_send_expects_reply(self):
        message = self.send()
        self.clock.return_value += retention.BODY_TTL * 3
        self.call('ack', id=message['id'])
        self.assertFalse(self.call('retention-sweep')['changed'])
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

    def test_one_way_real_time_needs_ack_push_needs_known_finish(self):
        message = self.send(one_way=True)
        self.sweep(retention.BODY_TTL * 2)
        self.assertIn(message['id'], self.state.messages)
        self.call('resolve', id=message['id'], state='submitted', note='manual acceptance only')
        self.sweep(retention.BODY_TTL * 4)
        self.assertIn(message['id'], self.state.messages)
        self.call('ack', id=message['id'])
        self.sweep(retention.BODY_TTL * 5)
        self.assertIn(message['id'], self.state.compact_messages)
        self.clock.return_value = CLOCK
        message = self.send('push', one_way=True)
        self.state.messages[message['id']]['route'].update(transport='codex', session=str(uuid.uuid4()))
        self.call('claim', id=message['id'])
        self.call('finish', id=message['id'], state='submitted', event='pushed')
        self.sweep(retention.BODY_TTL)
        self.assertIn(message['id'], self.state.compact_messages)

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

    def test_deadline_separate_from_wait_and_completion_late_reply_pinned(self):
        message = self.send(reply_deadline_seconds=60)
        self.call('ack', id=message['id'])
        self.sweep(61)
        self.assertEqual(self.call('show', id=message['id'])['request_status'], 'expired')
        self.assertEqual(self.state.messages[message['id']]['state'], 'received')
        self.sweep(60 + retention.BODY_TTL)
        self.assertIn(message['id'], self.state.compact_messages)
        frozen = deepcopy(message['return_route'])
        self.call('unregister', project='demo', name='alpha')
        reply = self.call('reply', id=message['id'], body='late answer', key='late')
        self.assertEqual(reply['route'], frozen)
        status = self.call('show', id=message['id'])
        self.assertEqual(status['request_status'], 'expired')
        self.assertTrue(status['late_reply'])
        self.assertTrue(status['body_expired'])
        self.assertEqual(next(m for m in self.call('conversation', id=message['id']) if m['id'] == reply['id'])['body'], 'late answer')
        with self.assertRaisesRegex(ValueError, 'body expired'):
            self.call('resolve', id=message['id'], state='done', note='Do not mutate compact rows')

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
        self.call('ack', id=message['id'])
        self.sweep(retention.BODY_TTL + 1)
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

    def test_durable_late_reply_preserves_compaction_anchor_and_pinned_route(self):
        message = self.send(reply_deadline_seconds=1)
        self.call('ack', id=message['id'])
        original_route = deepcopy(message['return_route'])
        self.sweep(retention.BODY_TTL + 1)
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

    def test_live_plan_only_uses_read_only_op(self):
        with patch.object(bridge.broker, 'request', return_value={'dry_run': True}) as request, redirect_stdout(io.StringIO()):
            self.assertEqual(bridge.main(['retention', '--live']), 0)
        self.assertEqual(request.call_args.args[1], {'op': 'retention-plan'})


if __name__ == '__main__':
    unittest.main()
