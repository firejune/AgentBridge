"""Confirmed archive cleanup uses frozen provider identity, never route names."""
import asyncio
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import bridge
import broker
import retention
import route_store


class ArchiveCorrespondenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-archive-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.store = route_store.RouteStore(Path(self.temp.name) / 'state')
        self.addCleanup(self.store.close)
        self.state = broker.State(store=self.store, max_messages=12)
        self.sid = str(uuid.uuid4())
        for project, name, sid in [('p', 'worker', self.sid), ('q', 'alias', self.sid),
                                   ('p', 'other', str(uuid.uuid4())), ('p', 'third', str(uuid.uuid4()))]:
            self.call('register', project=project, name=name, session=sid, cwd=self.temp.name, transport='codex')
        self.route = deepcopy(self.state.route('p', 'worker'))

    def call(self, op, **args):
        return self.state.action(dict(op=op, **args))

    def send(self, key, sender='worker', recipient='other'):
        return self.call('send', project='p', sender=sender, recipient=recipient, body=key, key=key)

    def replace_worker(self):
        self.call('unregister', project='p', name='worker')
        return self.call('register', project='p', name='worker', session=str(uuid.uuid4()),
                         cwd=self.temp.name, transport='codex')

    def purge(self, **args):
        return self.call('archive-correspondence', expected_route=self.route, **args)

    def test_both_frozen_endpoints_aliases_and_capacity_survive_restart(self):
        outgoing = self.send('out')
        incoming = self.send('in', 'other', 'worker')
        # Alias can be registered in another project and frozen into a message.
        self.call('register', project='q', name='peer', session=str(uuid.uuid4()), cwd=self.temp.name, transport='codex')
        alias = self.call('send', project='q', sender='peer', recipient='alias', body='alias', key='alias')
        survivor = self.send('keep', 'other', 'third')
        # Model an older reply whose original parent can be archived separately.
        del survivor['mcp_operation_send']
        survivor['reply_to'] = outgoing['id']
        survivor['note'] = 'preserve other-session annotation'
        expected = deepcopy(survivor)
        self.assertEqual(self.purge(check_only=True), dict(supported=True, matched=3, retained=0))
        result = self.purge(archived=True)
        self.assertEqual(result, dict(removed=3, retained=0, complete=True))
        self.assertEqual(self.state.messages, {survivor['id']: expected})
        self.assertEqual(set(self.state.keys), {'keep'})
        self.assertEqual(set(self.state.answers), {survivor['id']})
        self.assertFalse(any(e['message']['id'] in {outgoing['id'], incoming['id'], alias['id']} for e in self.state.events))
        restored = broker.State(store=self.store, max_messages=12)
        self.assertEqual(set(restored.messages), {survivor['id']})
        self.assertEqual(restored.messages[survivor['id']]['reply_to'], outgoing['id'])
        self.assertEqual(restored.endpoints, self.state.endpoints)
        self.state.max_messages = 2
        self.send('capacity')  # Freed pending mail would otherwise fill capacity.

    def test_reused_name_different_session_workspace_or_transport_is_untouched(self):
        old = self.send('old')
        replacement = self.replace_worker()
        fresh = self.send('replacement')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.purge(archived=True)
        self.route = deepcopy(replacement)
        result = self.purge(archived=True)
        self.assertEqual(result['removed'], 1)
        self.assertIn(old['id'], self.state.messages)
        self.assertNotIn(fresh['id'], self.state.messages)
        # Even an identical UUID cannot establish identity across providers/cwds.
        self.call('unregister', project='p', name='worker')
        self.call('register', project='p', name='worker', session=self.sid, cwd=self.temp.name, transport='agy')
        self.route = self.state.route('p', 'worker')
        self.assertEqual(self.purge(archived=True)['removed'], 0)
        old = self.state.messages[old['id']]
        old['route']['cwd'] = '/different-workspace'
        old['return_route']['cwd'] = '/different-workspace'
        self.call('unregister', project='p', name='worker')
        self.call('register', project='p', name='worker', session=self.sid, cwd=self.temp.name, transport='codex')
        self.route = self.state.route('p', 'worker')
        self.assertEqual(self.purge(archived=True)['removed'], 0)

    def test_requires_confirmed_archive_and_cas_without_writes(self):
        message = self.send('mail')
        before = self.state.correspondence.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'confirmed'):
            self.purge(archived=False)
        self.assertEqual(self.state.correspondence.path.read_bytes(), before)
        self.assertIn(message['id'], self.state.messages)
        self.replace_worker()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.purge(check_only=True)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.call('unregister-archived', project='p', name='worker', expected_route=self.route, archived=True)

    def test_uncertain_retained_and_active_claim_wait_and_retry_protected(self):
        uncertain = self.send('unknown')
        uncertain['state'] = 'uncertain'
        pending = self.send('pending')
        self.assertEqual(self.purge(check_only=True)['retained'], 1)
        self.state.waiters[uncertain['id']] = 1
        with self.assertRaisesRegex(ValueError, 'active'):
            self.purge(check_only=True)
        self.state.waiters.clear()
        self.assertEqual(self.purge(archived=True), dict(removed=1, retained=1, complete=False, active_retained=0))
        self.assertIn(uncertain['id'], self.state.messages)
        self.assertNotIn(pending['id'], self.state.messages)
        for mode in ('claim', 'wait', 'reply-wait', 'reserved'):
            with self.subTest(mode=mode):
                msg = self.send(mode)
                if mode == 'claim':
                    msg['state'] = 'dispatching'
                elif mode == 'wait':
                    self.state.waiters[msg['id']] = 1
                elif mode == 'reply-wait':
                    parent = self.send('parent-' + mode, 'other', 'third')
                    msg = self.state.messages[msg['id']]
                    msg['reply_to'] = parent['id']
                    self.state.waiters[parent['id']] = 1
                else:
                    msg['provider_retry'] = {'status':'reserved'}
                with self.assertRaisesRegex(ValueError, 'active'):
                    self.purge(check_only=True)
                # The same guard is re-evaluated after provider archival. No
                # destructive write to active work occurs even if it raced preflight.
                if mode != 'reserved':
                    self.assertEqual(self.purge(archived=True)['active_retained'], 1)
                    self.assertIn(msg['id'], self.state.messages)
                msg = self.state.messages[msg['id']]
                msg.pop('provider_retry', None)
                msg['state'] = 'pending'
                self.state.waiters.clear()
                self.purge(archived=True)

    def test_checkpoint_failure_has_no_memory_or_replay_mutation(self):
        message = self.send('retained')
        before = self.state.correspondence.path.read_bytes()
        events = deepcopy(list(self.state.events))
        with patch.object(self.state.correspondence, 'save', side_effect=OSError('disk failed')):
            with self.assertRaisesRegex(OSError, 'disk failed'):
                self.purge(archived=True)
        self.assertIn(message['id'], self.state.messages)
        self.assertEqual(list(self.state.events), events)
        self.assertEqual(self.state.correspondence.path.read_bytes(), before)

    def test_archive_removes_exact_compact_records_and_preserves_unrelated_dedup(self):
        self.state.retention_ttl = True
        archived = self.send('archive')
        unrelated = self.send('keep', 'other', 'third')
        for message in (archived, unrelated):
            self.call('resolve', id=message['id'], state='done', note='fixture completion')
        with patch.object(broker.time, 'time', return_value=time.time() + retention.BODY_TTL + 1):
            self.assertTrue(self.call('retention-sweep')['changed'])
            self.assertEqual(set(self.state.compact_messages), {archived['id'], unrelated['id']})
            self.state.waiters[archived['id']] = 1
            with self.assertRaisesRegex(ValueError, 'active'):
                self.purge(check_only=True)
            self.assertEqual(self.purge(archived=True), dict(removed=0, retained=1, complete=False, active_retained=1))
            self.state.waiters.clear()
            self.assertEqual(self.purge(archived=True), dict(removed=1, retained=0, complete=True))
            self.assertEqual(json.loads(self.state.correspondence.path.read_bytes())['version'], 2)
            self.state = broker.State(store=self.store, max_messages=12, retention_ttl=True)
            self.assertEqual(self.send('keep', 'other', 'third')['id'], unrelated['id'])
            self.assertEqual(set(self.state.compact_messages), {unrelated['id']})
            self.assertNotIn('archive', self.state.keys)

    def test_actual_wait_is_protected_until_waiter_exits(self):
        message = self.send('waiting')
        async def check():
            reader = asyncio.StreamReader()
            waiter = asyncio.create_task(broker.wait_answer(self.state, dict(id=message['id'], timeout=1), reader))
            await asyncio.sleep(0)
            with self.assertRaisesRegex(ValueError, 'active'):
                self.purge(check_only=True)
            self.assertEqual(self.purge(archived=True)['retained'], 1)
            reader.feed_eof()
            await waiter
            self.assertEqual(self.state.waiters, {})
            self.assertEqual(self.purge(archived=True)['removed'], 1)
        asyncio.run(check())

    def test_dequeued_watch_event_is_marked_but_ordinary_evictions_are_not(self):
        removed = self.send('dequeued')
        event = next(e for e in self.state.events if e['message']['id'] == removed['id'])
        async def check():
            queue = asyncio.Queue()
            queue.put_nowait(event)
            task = asyncio.create_task(queue.get())
            await asyncio.sleep(0)
            self.state.events.clear()  # Already outside replay retention.
            self.state.subscribers.append(dict(queue=queue, next_event=task))
            self.purge(archived=True)
            self.assertTrue(task.result()['_archive_purged'])
        asyncio.run(check())
        self.state.subscribers.clear()
        survivor = self.send('ordinary', 'other', 'third')
        event = next(e for e in self.state.events if e['message']['id'] == survivor['id'])
        self.state.messages[survivor['id']]['state'] = 'done'
        self.state.max_messages = 1
        self.state.make_room()
        self.assertNotIn('_archive_purged', event)

    def test_receipts_snapshot_invalidation_and_watch_buffers(self):
        removed = self.send('remove')
        keep = self.send('keep', 'other', 'third')
        owner, context = 'a' * 64, 'b' * 64
        expires = time.time() + 1000
        for m in (removed, keep):
            self.state.inbox_receipts[(owner, context, m['id'])] = {
                'expires':expires, 'result':dict(id=m['id'], acknowledged=True, acknowledged_at=broker.now(),
                                               broker_instance=self.state.instance, reconcile_until=expires)}
        for label, items in [('affected', [removed, keep]), ('unrelated', [keep])]:
            metadata = [self.state.inbox_metadata(m, 'fixture') for m in items]
            self.state.inbox_snapshots[label] = dict(owner=owner, context=context, expires=expires,
                items=metadata, bytes=len(json.dumps(metadata, ensure_ascii=False).encode()))
        queue = asyncio.Queue()
        removed_event = next(e for e in self.state.events if e['message']['id'] == removed['id'])
        keep_event = next(e for e in self.state.events if e['message']['id'] == keep['id'])
        queue.put_nowait(removed_event)
        queue.put_nowait(keep_event)
        backlog = [deepcopy(removed_event), deepcopy(keep_event)]
        self.state.subscribers.append(dict(queue=queue, backlog=backlog))
        self.purge(archived=True)
        self.assertEqual(set(self.state.inbox_snapshots), {'unrelated'})
        self.assertEqual({k[2] for k in self.state.inbox_receipts}, {keep['id']})
        self.assertTrue(backlog[0]['_archive_purged'])
        self.assertNotIn('_archive_purged', backlog[1])
        self.assertEqual(queue.get_nowait()['message']['id'], keep['id'])
        self.assertTrue(queue.empty())


class ArchiveCloseCommandTests(unittest.TestCase):
    def setUp(self):
        self.route = dict(project='p', name='worker', transport='codex', session=str(uuid.uuid4()), cwd='/tmp')
        self.calls = []
        self.archive_result = dict(closed=True, archived=True, session=self.route['session'])
        self.cleanup_result = dict(removed=2, retained=0, complete=True)
        self.request_error = None

    def request(self, socket, args, **kwargs):
        self.calls.append(deepcopy(args))
        if args['op'] == 'routes':
            return [self.route]
        if args['op'] == 'archive-correspondence':
            if self.request_error and (self.request_error[0] == 'preflight' or not args.get('check_only')):
                raise self.request_error[1]
            return dict(supported=True, matched=2, retained=0) if args.get('check_only') else self.cleanup_result
        return self.route

    def run_close(self, *flags):
        out = io.StringIO()
        with patch.object(bridge.broker, 'request', side_effect=self.request), \
                patch.object(bridge, 'prepare_retro', return_value=None), \
                patch.object(bridge, 'close_session', return_value=self.archive_result) as close, \
                patch('sys.stdout', out):
            code = bridge.main(['close', 'worker', '--project', 'p', '--no-retro', 'fixture', *flags])
        return code, json.loads(out.getvalue()), close

    def test_confirmed_archive_cleanup_and_keep_route(self):
        code, result, _ = self.run_close('--archive', '--keep-route')
        self.assertEqual((code, result['archived'], result['route_removed']), (0, True, False))
        self.assertEqual([a['op'] for a in self.calls], ['routes', 'archive-correspondence', 'archive-correspondence'])
        self.calls.clear()
        code, result, _ = self.run_close('--archive')
        self.assertEqual((code, result['route_removed']), (0, True))
        self.assertEqual(self.calls[-1]['expected_route'], self.route)

    def test_ordinary_close_preserves_mail(self):
        self.archive_result['archived'] = False
        code, result, _ = self.run_close()
        self.assertEqual((code, result['route_removed']), (0, True))
        self.assertEqual([a['op'] for a in self.calls], ['routes', 'unregister-if-current'])
        self.assertEqual(self.calls[-1]['expected_route'], self.route)

    def test_old_broker_refuses_before_provider_action(self):
        self.request_error = ('preflight', ValueError('Unknown operation'))
        code, result, close = self.run_close('--archive')
        self.assertEqual(code, 1)
        self.assertIn('upgrade', result['error'])
        close.assert_not_called()

    def test_unconfirmed_archive_never_purges_or_unregisters(self):
        self.archive_result['archived'] = False
        code, result, _ = self.run_close('--archive')
        self.assertEqual((code, result['route_removed']), (1, False))
        self.assertEqual(len(self.calls), 2)
        self.assertIn('not confirmed', result['cleanup_error'])

    def test_partial_archive_exits_nonzero_even_if_confirmed_root_route_is_removed(self):
        self.archive_result.update(archive_incomplete=True, count=1,
                                   results=[{'status': 'not_archived', 'archived': True}])
        code, result, _ = self.run_close('--archive')
        self.assertEqual((code, result['archived'], result['route_removed'], result['count']),
                         (1, True, True, 1))
        self.assertEqual(self.calls[-1]['op'], 'unregister-archived')

    def test_failed_purge_reports_confirmed_archive_without_retry(self):
        self.request_error = ('purge', OSError('socket disconnected'))
        code, result, _ = self.run_close('--archive')
        self.assertEqual((code, result['archived'], result['route_removed']), (1, True, False))
        self.assertTrue(result['cleanup_uncertain'])
        self.assertEqual(len(self.calls), 3)

    def test_provider_archive_failure_never_purges_or_unregisters(self):
        out = io.StringIO()
        with patch.object(bridge.broker, 'request', side_effect=self.request), \
                patch.object(bridge, 'prepare_retro', return_value=None), \
                patch.object(bridge, 'close_session', side_effect=ValueError('Archive failed')) as close, \
                patch('sys.stdout', out):
            code = bridge.main(['close', 'worker', '--project', 'p', '--archive', '--no-retro', 'fixture'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())['error'], 'Archive failed')
        self.assertEqual(len(self.calls), 2)
        close.assert_called_once()

    def test_uncertain_only_cleanup_reports_retained_and_succeeds(self):
        self.cleanup_result = dict(removed=1, retained=1, complete=False, active_retained=0)
        for flags in ((), ('--keep-route',)):
            with self.subTest(flags=flags):
                code, result, _ = self.run_close('--archive', *flags)
                self.assertEqual((code, result['archived'], result['route_removed']), (0, True, not flags))
                self.assertEqual(result['correspondence_cleanup'], self.cleanup_result)
                self.assertNotIn('cleanup_error', result)

    def test_active_or_unclassified_retention_is_reported_as_incomplete(self):
        for extra in ({'active_retained': 1}, {}):
            with self.subTest(extra=extra):
                self.cleanup_result = dict(removed=1, retained=1, complete=False, **extra)
                code, result, _ = self.run_close('--archive')
                self.assertEqual((code, result['archived'], result['route_removed']), (1, True, True))
                self.assertEqual(result['correspondence_cleanup'], self.cleanup_result)
                self.assertIn('protected correspondence remains', result['cleanup_error'])

    def test_old_broker_between_cleanup_and_removal_refuses_new_operation(self):
        original = self.request
        def request(socket, args, **kwargs):
            if args['op'] == 'unregister-archived':
                self.calls.append(deepcopy(args))
                raise ValueError('Unknown operation')
            if args['op'] == 'unregister':
                self.fail('Archive cleanup must never fall back to unguarded unregister')
            return original(socket, args, **kwargs)
        with patch.object(self, 'request', side_effect=request):
            code, result, _ = self.run_close('--archive')
        self.assertEqual((code, result['archived'], result['route_removed']), (1, True, False))
        self.assertTrue(result['correspondence_cleanup']['complete'])
        self.assertEqual(self.calls[-1]['op'], 'unregister-archived')
        self.assertEqual(result['route_error'], 'Unknown operation')

    def test_ordinary_close_io_failure_preserves_existing_error_result(self):
        original = self.request
        def request(socket, args, **kwargs):
            if args['op'] == 'unregister-if-current':
                raise OSError('socket disconnected')
            return original(socket, args, **kwargs)
        with patch.object(self, 'request', side_effect=request):
            code, result, _ = self.run_close()
        self.assertEqual((code, result), (1, {'error':'socket disconnected'}))

    def test_partial_cleanup_and_route_failure_are_both_reported(self):
        self.cleanup_result = dict(removed=1, retained=1, complete=False, active_retained=0)
        original = self.request
        def request(socket, args, **kwargs):
            if args['op'] == 'unregister-archived':
                raise ValueError('Endpoint is missing or changed')
            return original(socket, args, **kwargs)
        with patch.object(self, 'request', side_effect=request):
            code, result, _ = self.run_close('--archive')
        self.assertEqual((code, result['route_removed']), (1, False))
        self.assertEqual(result['correspondence_cleanup']['retained'], 1)
        self.assertIn('route removal was not confirmed', result['cleanup_error'])
        self.assertIn('changed', result['route_error'])

    def test_route_removal_failure_keeps_successful_cleanup_receipt(self):
        original = self.request
        def request(socket, args, **kwargs):
            if args['op'] == 'unregister-archived':
                raise ValueError('Endpoint is missing or changed')
            return original(socket, args, **kwargs)
        with patch.object(self, 'request', side_effect=request):
            code, result, _ = self.run_close('--archive')
        self.assertEqual((code, result['archived'], result['route_removed']), (1, True, False))
        self.assertTrue(result['correspondence_cleanup']['complete'])
        self.assertIn('changed', result['route_note'])


if __name__ == '__main__':
    unittest.main()
