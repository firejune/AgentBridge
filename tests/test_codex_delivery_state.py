"""Issue #153: what `show` and `ping` say about a Codex delivery, derived in the CLI."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import codex_live
import codex_preview
import codex_queue


def run(*argv):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = bridge.main(list(argv))
    return code, json.loads(stdout.getvalue())


class ShowReadByRecipientTests(unittest.TestCase):
    def shown(self, transport, state):
        message = {'id': str(uuid.uuid4()), 'state': state, 'route': {'transport': transport, 'session': 's'}}
        with patch.object(bridge.broker, 'request', return_value=dict(message)) as request:
            code, result = run('--socket', 'test.sock', 'show', message['id'])
        self.assertEqual(request.call_args.args[1], {'op': 'show', 'id': message['id']})
        self.assertEqual(code, 0)
        return result

    def test_codex_submitted_is_unread_and_received_or_later_is_read(self):
        for state, expected in (('submitted', False), ('received', True), ('in_progress', True),
                                ('answered', True), ('done', True)):
            with self.subTest(state=state):
                self.assertEqual(self.shown('codex', state)['read_by_recipient'], expected)

    def test_other_states_and_transports_carry_no_derived_field(self):
        for transport, state in (('codex', 'pending'), ('codex', 'dispatching'), ('codex', 'uncertain'),
                                 ('codex', 'failed'), ('claude-peer', 'submitted'), ('agy', 'received'),
                                 ('realtime', 'submitted')):
            with self.subTest(transport=transport, state=state):
                self.assertNotIn('read_by_recipient', self.shown(transport, state))


class CodexDeliveryHealthTests(unittest.TestCase):
    def setUp(self):
        self.client = patch.object(codex_live, 'Client').start()
        self.stores = patch.object(codex_queue, 'default_stores',
                                   return_value=(Path('/private/state'), Path('/private/queue'))).start()
        self.owned = patch.object(codex_preview, 'require_owned_store').start()
        self.owner = patch.object(codex_queue, 'desktop_queue_owner', return_value=('1', '2', 'app')).start()
        self.addCleanup(patch.stopall)
        self.unavailable = codex_live.EndpointUnavailable

    def absent(self):
        self.client.return_value.connect.side_effect = self.unavailable(
            self.unavailable.DEFAULT, self.unavailable.MISSING)

    def test_a_connecting_ipc_socket_is_the_ipc_path_and_nothing_is_sent(self):
        self.assertEqual(bridge.codex_delivery_health(), {'path': 'ipc', 'reason': None})
        client = self.client.return_value
        client.connect.assert_called_once_with()
        client.close.assert_called_once_with()
        client.call.assert_not_called()
        client.write.assert_not_called()
        self.owner.assert_not_called()

    def test_an_absent_socket_with_an_owned_queue_and_one_owner_is_the_queue_path(self):
        for reason in (self.unavailable.MISSING, self.unavailable.REFUSED):
            with self.subTest(reason=reason):
                self.client.return_value.connect.side_effect = self.unavailable(self.unavailable.DEFAULT, reason)
                self.assertEqual(bridge.codex_delivery_health(), {'path': 'desktop-queue', 'reason': None})
        self.owned.assert_called_with(Path('/private/queue'))

    def test_each_failed_check_names_itself_without_private_details(self):
        cases = (
            ('stores', ValueError('non-default Desktop configuration'), 'non-default Codex configuration'),
            ('owned', ValueError('/private/queue'), 'queue store missing or not owned'),
            ('owned', FileNotFoundError(2, '/private/queue'), 'queue store missing or not owned'),
            ('owner', None, 'no single Desktop queue owner'),
            ('owner', subprocess.CalledProcessError(1, ['/usr/sbin/lsof']), 'no single Desktop queue owner'),
            ('owner', ValueError('not enough values'), 'no single Desktop queue owner'),
            ('owner', subprocess.TimeoutExpired(['/bin/ps'], 1), 'queue owner check timed out'),
        )
        for where, effect, reason in cases:
            with self.subTest(where=where, reason=reason):
                self.absent()
                target = {'stores': self.stores, 'owned': self.owned, 'owner': self.owner}[where]
                original = (target.side_effect, target.return_value)
                if effect is None:
                    target.return_value = None
                else:
                    target.side_effect = effect
                try:
                    self.assertEqual(bridge.codex_delivery_health(), {'path': 'unavailable', 'reason': reason})
                finally:
                    target.side_effect, target.return_value = original

    def test_an_unusable_or_configured_socket_does_not_fall_back_to_the_queue(self):
        for name, reason, expected in (
                (self.unavailable.DEFAULT, self.unavailable.UNSAFE, 'Codex IPC socket unusable'),
                (self.unavailable.DEFAULT, self.unavailable.TIMEOUT, 'Codex IPC socket unusable'),
                (self.unavailable.CONFIGURED, self.unavailable.MISSING, 'non-default Codex configuration')):
            with self.subTest(name=name, reason=reason):
                self.client.return_value.connect.side_effect = self.unavailable(name, reason)
                self.assertEqual(bridge.codex_delivery_health(), {'path': 'unavailable', 'reason': expected})
        self.owner.assert_not_called()

    def test_the_owner_reads_share_one_bounded_budget(self):
        self.absent()
        timeouts = []

        def owner(queue, read):
            for argv in (['/usr/sbin/lsof'], ['/bin/ps']):
                read(argv)
            return '1', '2', 'app'

        self.owner.side_effect = owner
        with patch.object(codex_queue, 'checked_output',
                          side_effect=lambda argv, timeout: timeouts.append(timeout) or '') as checked:
            self.assertEqual(bridge.codex_delivery_health(), {'path': 'desktop-queue', 'reason': None})
        self.assertEqual(checked.call_count, 2)
        self.assertTrue(all(0 < value <= bridge.CODEX_HEALTH_SECONDS for value in timeouts), timeouts)
        self.client.assert_called_once_with(timeout=bridge.CODEX_HEALTH_SECONDS / 4)
        with patch.object(bridge.time, 'monotonic', side_effect=[0, 99, 99, 99]), \
                patch.object(codex_queue, 'checked_output') as checked:
            self.assertEqual(bridge.codex_delivery_health(),
                             {'path': 'unavailable', 'reason': 'queue owner check timed out'})
        checked.assert_not_called()

    def test_an_unexpected_error_in_any_helper_never_raises(self):
        self.client.side_effect = RuntimeError('/private/ipc.sock')
        self.assertEqual(bridge.codex_delivery_health(), {'path': 'unavailable', 'reason': 'health check failed'})


class PingCodexDeliveryTests(unittest.TestCase):
    def test_ping_reports_health_on_macos_only(self):
        health = {'path': 'desktop-queue', 'reason': None}
        with patch.object(bridge.broker, 'request',
                          side_effect=lambda *_, **__: {'instance': 'x', 'build': bridge.broker.build_id()}), \
                patch.object(bridge, 'codex_delivery_health', return_value=health) as check:
            with patch.object(bridge.sys, 'platform', 'darwin'):
                code, result = run('--socket', 'test.sock', 'ping')
            self.assertEqual((code, result['codex_delivery']), (0, health))
            with patch.object(bridge.sys, 'platform', 'linux'):
                code, result = run('--socket', 'test.sock', 'ping')
            self.assertEqual(code, 0)
            self.assertNotIn('codex_delivery', result)
        check.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
