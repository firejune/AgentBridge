"""Delivery outcomes through the CLI, an isolated broker, and synthetic providers."""
import asyncio
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import uuid

import agy_live
import bridge
import broker
import codex_live
import codex_queue
import codex_wake_open
import delivery
from support import RunningBroker
from test_agy_live import CSRF, FakeAgyServer
from test_codex_live import FakeDesktop


RESTART_NOTE = 'Broker restarted during delivery; resolve explicitly'


class DeliveryMatrixTests(unittest.TestCase):
    def setUp(self):
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.server = contexts.enter_context(RunningBroker())
        contexts.enter_context(patch.dict(os.environ, {'CODEX_HOME': self.server.temp.name}))
        self.verify_build = contexts.enter_context(patch.object(codex_wake_open, '_verify_app_build'))
        self.open_wake = contexts.enter_context(patch.object(codex_wake_open, '_open_wake_url'))
        contexts.enter_context(patch.object(codex_live, 'exact_saved_workspace', return_value=(None, None)))
        self.thread = str(uuid.uuid4())
        self.register_routes()

    def register_routes(self):
        for name, transport in (('sender', 'realtime'), ('codex', 'codex'), ('agy', 'agy'),
                                ('claude', 'claude-peer')):
            self.register(name, transport)

    def restart(self):
        self.server.restart()
        self.register_routes()

    def register(self, name, transport):
        self.server.call('register', project='demo', name=name, transport=transport,
                         cwd=self.server.temp.name, session=self.thread)

    def post(self, transport, key='message'):
        return self.server.call('send', project='demo', sender='sender', recipient=transport,
                                key=f'{transport}-{key}', body='Fixture body')

    def cli(self, *args, stdin=''):
        stream = io.StringIO()
        with redirect_stdout(stream), patch('sys.stdin', io.StringIO(stdin)):
            code = bridge.main(['--socket', self.server.path, *args])
        return code, json.loads(stream.getvalue()) if stream.getvalue() else None

    def show(self, message):
        return self.server.call('show', id=message['id'])

    def assert_record(self, message, state, note, event=None):
        record = self.show(message)
        self.assertEqual((record['state'], record['note']), (state, note))
        final = record['events'][-1]
        self.assertEqual((final['state'], final['note'], final.get('event')), (state, note, event))
        return record

    def provider_receipt(self, transport):
        if transport == 'codex':
            return {'delivery_mode': 'active', 'turn_id': 'fixture-turn'}
        return {'session': self.thread, 'pid': 42}

    def test_lost_claim_reply_current_behavior(self):
        for transport, provider in (('codex', codex_live), ('agy', agy_live)):
            with self.subTest(transport=transport):
                message = self.post(transport)
                original = broker.request

                def lose_claim(path, args, **fields):
                    result = original(path, args, **fields)
                    if args['op'] == 'claim':
                        raise TimeoutError('fixture claim reply lost')
                    return result

                with patch.object(broker, 'request', side_effect=lose_claim), \
                        patch.object(provider, 'deliver') as send:
                    self.assertEqual(self.cli('deliver', message['id']),
                                     (1, {'error': 'fixture claim reply lost'}))
                send.assert_not_called()
                self.assert_record(message, 'dispatching', None)
                # Restart converts the committed reservation without replaying it.
                self.restart()
                record = self.show(message)
                self.assertEqual((record['state'], record['note']), ('uncertain', RESTART_NOTE))
                self.assertNotIn('pushed', [entry.get('event') for entry in record['events']])

    def test_lost_finish_reply_current_behavior(self):
        for transport, provider in (('codex', codex_live), ('agy', agy_live)):
            for committed in (False, True):
                with self.subTest(transport=transport, committed=committed):
                    message = self.post(transport, key=str(committed))
                    original = broker.request

                    def lose_finish(path, args, **fields):
                        if args['op'] == 'finish':
                            if committed:
                                original(path, args, **fields)
                            raise TimeoutError('fixture finish reply lost')
                        return original(path, args, **fields)

                    with patch.object(broker, 'request', side_effect=lose_finish), \
                            patch.object(provider, 'deliver', return_value=self.provider_receipt(transport)) as send:
                        self.assertEqual(self.cli('deliver', message['id']),
                                         (1, {'error': 'fixture finish reply lost'}))
                    send.assert_called_once()
                    if committed:
                        note = ('Codex active tool output accepted by turn fixture-turn' if transport == 'codex'
                                else f'pushed to Antigravity session {self.thread} (pid 42)')
                        self.assert_record(message, 'submitted', note, 'pushed')
                    else:
                        self.assert_record(message, 'dispatching', None)
                        self.restart()
                        record = self.show(message)
                        self.assertEqual((record['state'], record['note']), ('uncertain', RESTART_NOTE))
                        self.assertNotIn('pushed', [entry.get('event') for entry in record['events']])
                        self.server.call('resolve', id=message['id'], state='done', note='fixture reconciled')

    def test_busy_claim_stays_pending_without_automatic_delivery_current_behavior(self):
        for transport, provider in (('codex', codex_live), ('agy', agy_live)):
            with self.subTest(transport=transport):
                blocker = self.post(transport, key='blocker')
                self.server.call('claim', id=blocker['id'])
                second = self.post(transport, key='second')
                before = self.show(second)
                note = f"pending behind {blocker['id']} since {before['blocked_by']['since']}"
                self.assertEqual(before['note'], note)
                self.assertEqual(before['blocked_by']['state'], 'dispatching')
                with patch.object(provider, 'deliver') as send:
                    self.assertEqual(self.cli('deliver', second['id']), (1, {
                        'error': f"Recipient has an active or uncertain delivery. Message {second['id']} {note}"}))
                    self.assertEqual(self.show(second), before)
                    self.server.call('finish', id=blocker['id'], state='submitted', note='fixture accepted', event='pushed')
                    after = self.show(second)
                    self.assertEqual(after['state'], 'pending')
                    self.assertEqual(after['events'], before['events'])
                    self.assertIsNone(after['note'])
                    self.assertNotIn('blocked_by', after)
                    send.assert_not_called()

    def test_late_success_after_restart_reports_uncertain_for_each_transport(self):
        cases = (('codex', codex_live, self.provider_receipt('codex')),
                 ('codex', codex_live, {'delivery_mode': 'queue', 'delivery_path': codex_queue.DELIVERY_PATH}),
                 ('agy', agy_live, self.provider_receipt('agy')))
        for transport, provider, receipt in cases:
            with self.subTest(transport=transport, receipt=receipt):
                mode = receipt.get('delivery_mode', 'agy')
                message = {}

                def restart_during_write(route, mid, *args, **kwargs):
                    message['id'] = mid
                    self.assert_record(message, 'dispatching', None)
                    self.restart()
                    return receipt

                with patch.object(provider, 'deliver', side_effect=restart_during_write) as send:
                    code, result = self.cli('send', '--project', 'demo', '--from', 'sender', '--to', transport,
                                            '--key', mode, '--file', '-', stdin='Fixture body')
                send.assert_called_once()
                record = self.show(message)
                self.server.call('resolve', id=message['id'], state='done', note='fixture reconciled')
                self.assertEqual(code, 1, result)
                self.assertEqual(result['message']['state'], 'uncertain')
                self.assertEqual(result['message']['id'], message['id'])
                self.assertIs(result['push_failed'], True)
                self.assertIn('Inspect the message', result['error'])
                self.assertNotIn('delivery_path', result)
                self.assertEqual((record['state'], record['note']), ('uncertain', RESTART_NOTE))
                self.assertNotIn('pushed', [entry.get('event') for entry in record['events']])

    def test_agy_http_404_stays_uncertain_current_behavior(self):
        message = self.post('agy')
        server = {'url': 'http://127.0.0.1:1', 'csrf': CSRF, 'pid': 42}
        target = {'cwd': self.server.temp.name, 'status': 'idle'}
        refusal = HTTPError(server['url'], 404, 'fixture not found', None, io.BytesIO(b'{}'))
        with patch.object(agy_live, 'resolve_agy', return_value=(server, target)), \
                patch.object(agy_live.urllib.request, 'urlopen', side_effect=refusal) as request:
            code, result = self.cli('deliver', message['id'])
        request.assert_called_once()
        self.assertEqual(code, 1, result)
        note = 'Antigravity SendAgentMessage HTTP 404'
        self.assertEqual(result['error'], note)
        self.assert_record(message, 'uncertain', note)

    def test_agy_wake_and_title_failures_leave_accepted_message_submitted_current_behavior(self):
        message = self.post('agy')
        server = {'url': 'http://127.0.0.1:1', 'csrf': CSRF, 'pid': 42}
        target = {'cwd': self.server.temp.name, 'status': 'idle'}
        methods = []

        def rpc(url, csrf, method, payload, timeout):
            methods.append(method)
            if method != 'SendAgentMessage':
                raise agy_live.DeliveryError('fixture auxiliary RPC refused')
            return {}

        with patch.object(agy_live, 'resolve_agy', return_value=(server, target)), \
                patch.object(agy_live, 'rpc_call', side_effect=rpc), \
                patch.object(agy_live, 'wake_session', wraps=agy_live.wake_session) as wake:
            code, result = self.cli('deliver', message['id'])
        self.assertEqual(code, 0, result)
        wake.assert_called_once()
        self.assertEqual(methods, ['SendAgentMessage', 'SendUserCascadeMessage', 'UpdateConversationAnnotations'])
        record = self.assert_record(message, 'submitted',
                                    f'pushed to Antigravity session {self.thread} (pid 42)', 'pushed')
        self.assertNotIn('fixture auxiliary RPC refused', json.dumps(record))

    def queue_delivery(self, message, *, refusal=None, receipt=None):
        endpoint = codex_live.EndpointUnavailable
        process = Mock(returncode=0)
        process.poll.return_value = 0
        process.communicate.return_value = (receipt, None)
        with patch.object(broker, 'DEFAULT_SOCKET', Path(self.server.path)), \
                patch.object(codex_live, 'Client') as client, \
                patch.object(codex_live, 'exact_saved_workspace', return_value=(None, None)), \
                patch.object(codex_queue, 'check_route', side_effect=refusal), \
                patch.object(codex_queue.subprocess, 'Popen', return_value=process) as queue:
            client.return_value.mutation_sent = False
            client.return_value.__enter__.side_effect = endpoint(endpoint.DEFAULT, endpoint.MISSING)
            result = self.cli('deliver', message['id'])
        return result, queue

    def test_queue_receipt_accepts_exact_wire_text_and_leaves_unopened_pointer_unread(self):
        message = self.post('codex')
        queued_id = uuid.uuid4()
        receipt = f'Queued message {queued_id} for thread {self.thread}.\n'

        def open_wake(thread, timeout):
            self.assertEqual(thread, self.thread)
            self.assertGreater(timeout, 0)
            self.assertEqual(self.show(message)['state'], 'submitted')

        self.open_wake.side_effect = open_wake
        (code, result), queue = self.queue_delivery(message, receipt=receipt)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['delivery_path'], codex_queue.DELIVERY_PATH)
        self.assertIs(result['wake_requested'], True)
        self.open_wake.assert_called_once()
        queue.assert_called_once()
        notification = queue.call_args.args[0][-1]
        self.assertIn(message['id'], notification)
        self.assertNotIn(message['body'], notification)
        record = self.assert_record(message, 'submitted',
                                    'Codex Desktop queue accepted the Agent Bridge notification', 'pushed')
        code, shown = self.cli('show', message['id'])
        self.assertEqual(code, 0, shown)
        self.assertIs(shown['read_by_recipient'], False)
        self.assertEqual(shown['events'], record['events'])

        self.restart()
        with self.assertRaisesRegex(ValueError, 'already accepted'):
            self.server.call('resolve', id=message['id'], state='pending', note='fixture resolution')
        self.assertEqual(self.cli('deliver', message['id']),
                         (1, {'error': 'Only pending messages may be delivered'}))
        self.open_wake.assert_called_once()

    def test_unconfirmed_queue_receipt_never_opens_wake(self):
        malformed = self.post('codex', key='malformed')
        receipt = f'Queued message {uuid.uuid4()} for thread {self.thread}!\n'
        (code, result), queue = self.queue_delivery(malformed, receipt=receipt)
        self.assertEqual(code, 1, result)
        queue.assert_called_once()
        self.assert_record(malformed, 'uncertain', codex_queue.UNKNOWN)
        self.open_wake.assert_not_called()

    def test_queue_acceptance_survives_wake_refusal_and_opener_failure(self):
        unsupported = 'This Codex Desktop build has not been verified for queue wake'
        opener_failed = 'The Codex Desktop wake request could not be confirmed'
        self.open_wake.side_effect = OSError('private opener diagnostics')
        for reason in (unsupported, opener_failed):
            with self.subTest(reason=reason):
                self.verify_build.side_effect = codex_wake_open.WakeError(reason) if reason == unsupported else None
                message = self.post('codex', key=reason)
                receipt = f'Queued message {uuid.uuid4()} for thread {self.thread}.\n'
                (code, result), queue = self.queue_delivery(message, receipt=receipt)
                self.assertEqual((code, result['state'], result['wake_requested'], result['wake_reason']),
                                 (0, 'submitted', False, reason))
                self.assertEqual(self.show(message)['state'], 'submitted')
                queue.assert_called_once()
                self.assertEqual(self.open_wake.call_count, int(reason == opener_failed))

    def test_queue_refusal_drops_private_reason_current_behavior(self):
        message = self.post('codex')
        (code, result), queue = self.queue_delivery(message, refusal=ValueError('fixture private queue check'))
        self.assertEqual(code, 1, result)
        queue.assert_not_called()
        self.open_wake.assert_not_called()
        record = self.assert_record(message, 'pending', result['error'], 'push_failed')
        self.assertNotIn('fixture private queue check', json.dumps(record))
        self.assertEqual(result['error'], codex_live.EndpointUnavailable(
            codex_live.EndpointUnavailable.DEFAULT, codex_live.EndpointUnavailable.MISSING).args[0])

    def test_claude_interrupt_when_finish_fails_current_behavior(self):
        message = self.post('claude')
        original = broker.request

        def lose_finish(path, args, **fields):
            if args['op'] == 'finish':
                raise TimeoutError('fixture finish reply lost')
            return original(path, args, **fields)

        target = {'id': self.thread}
        with patch.object(broker, 'request', side_effect=lose_finish), \
                patch.object(delivery.sessions, 'resolve_claude_peer', return_value=target), \
                patch.object(delivery, 'push_frame', side_effect=KeyboardInterrupt) as send:
            self.assertEqual(self.cli('deliver', message['id']), (130, None))
        send.assert_called_once()
        self.assert_record(message, 'dispatching', None)

    def test_exact_idle_rejection_starts_only_the_pinned_thread(self):
        message = self.post('codex')
        with FakeDesktop('idle') as desktop, patch.dict(os.environ, {'AGENT_BRIDGE_CODEX_IPC': desktop.path}):
            code, result = self.cli('deliver', message['id'])
        self.assertEqual(code, 0, result)
        self.assert_record(message, 'submitted', 'Codex idle tool output accepted by turn idle-turn', 'pushed')
        mutations = [call for call in desktop.calls if call.get('method', '').startswith('thread-follower-')]
        self.assertEqual([call['method'] for call in mutations],
                         ['thread-follower-steer-turn', 'thread-follower-start-turn'])
        self.assertTrue(all(call['targetClientId'] == desktop.owner for call in mutations))
        self.assertEqual(mutations[1]['params']['turnStart']['request']['threadId'], self.thread)

    def test_utf8_body_is_delivered_at_limit_and_rejected_one_byte_over(self):
        body = '🧪' * 8191 + '한' + 'x'
        self.assertEqual(len(body.encode()), 32768)
        agy_env = {'AGENT_BRIDGE_AGY_URL': '', 'AGENT_BRIDGE_AGY_CSRF': CSRF}
        with FakeDesktop() as desktop, FakeAgyServer(self.server.temp.name, self.thread) as agy:
            agy_env['AGENT_BRIDGE_AGY_URL'] = agy.url
            agy.trajectories[self.thread]['status'] = 'CASCADE_RUN_STATUS_IDLE'
            with patch.dict(os.environ, {**agy_env, 'AGENT_BRIDGE_CODEX_IPC': desktop.path}):
                for transport in ('codex', 'agy'):
                    with self.subTest(transport=transport):
                        args = ('send', '--project', 'demo', '--from', 'sender', '--to', transport)
                        code, result = self.cli(*args, '--key', 'at-' + transport, '--file', '-', stdin=body)
                        self.assertEqual(code, 0, result)
                        record = self.show(result)
                        self.assertEqual(record['body'], body)
                        self.assertEqual(record['state'], 'submitted')
                        self.assertEqual(record['events'][-1]['event'], 'pushed')
                        before = self.server.call('ping')['messages']
                        code, result = self.cli(*args, '--key', 'over-' + transport, '--file', '-', stdin=body + 'x')
                        self.assertEqual(code, 1, result)
                        self.assertIn('32768', result['error'])
                        self.assertEqual(self.server.call('ping')['messages'], before)
        steer, = (call for call in desktop.calls if call.get('method') == 'thread-follower-steer-turn')
        sent, = (payload for path, _, payload in agy.requests if path.endswith('/SendAgentMessage'))
        for envelope in (steer['params']['toolOutput']['output'], sent['content']):
            self.assertIn('\n\n' + body + '\n\n', envelope)


class DispatchExpiryDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_sweep_failure_reports_only_fixed_event_current_behavior(self):
        state = broker.State()
        recovered = asyncio.Event()

        async def sweep(args):
            self.assertEqual(args, {'op': 'dispatch-expire'})
            if not recovered.is_set():
                recovered.set()
                raise OSError('fixture private disk error')
            raise asyncio.CancelledError

        state.action_async = sweep
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            task = asyncio.create_task(broker.dispatch_loop(state, interval=0))
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(stderr.getvalue(), '{"event": "dispatch_expire_failed"}\n')
