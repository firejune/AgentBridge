"""Cooperative Codex stop contracts on a private broker and config directory."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from support import RunningBroker

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'
sys.path.insert(0, str(SCRIPTS))
import bridge
import broker
import codex_hook
import codex_stop_hook

THREAD = '00000000-0000-4000-8000-000000000101'
OTHER = '00000000-0000-4000-8000-000000000102'


def event(thread=THREAD, name='PreToolUse'):
    return json.dumps({'session_id': thread, 'hook_event_name': name}).encode()


class StopBrokerTests(unittest.TestCase):
    def setUp(self):
        self.state = broker.State()
        self.state.endpoints['demo', 'worker'] = {
            'project': 'demo', 'name': 'worker', 'session': THREAD, 'transport': 'codex',
            'cwd': '/tmp/example', 'executable': None,
        }

    def request(self, **overrides):
        return self.state.action(dict(op='codex-stop', project='demo', name='worker', **overrides))

    def claim(self, session=THREAD, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + 0.25
        return self.state.action({'op': 'codex-stop-claim', 'session': session, 'deadline': deadline})

    def finish(self, id):
        return self.state.action({'op': 'codex-stop-finish', 'session': THREAD, 'id': id})

    def test_matching_claim_is_exclusive_until_finished(self):
        requested = self.request(reason='Pause')
        self.assertEqual(requested['state'], 'requested')
        self.assertIsNone(self.claim(OTHER))
        self.assertFalse(self.finish(requested['id']))
        self.assertEqual(self.claim()['id'], requested['id'])
        self.assertIsNone(self.claim())
        self.assertFalse(self.finish(OTHER))
        self.assertTrue(self.finish(requested['id']))
        self.assertIsNone(self.claim())
        self.assertNotEqual(self.request()['id'], requested['id'])

    def test_stale_claim_cannot_consume_and_expired_request_is_cleared(self):
        with patch.object(broker.time, 'monotonic', return_value=10) as clock:
            self.request()
            self.assertIsNone(self.claim(deadline=9))
            self.assertEqual(len(self.state.codex_stops), 1)
            clock.return_value = 10 + broker.CODEX_STOP_TTL
            self.assertIsNone(self.claim())
            self.assertEqual(self.state.codex_stops, {})

    def test_removed_or_replaced_route_is_ineligible(self):
        self.request()
        self.state.endpoints['demo', 'worker'] = {**self.state.endpoints['demo', 'worker'], 'session': OTHER}
        self.assertIsNone(self.claim())
        self.assertIsNone(self.claim(OTHER))
        self.state.endpoints.clear()
        with self.assertRaisesRegex(ValueError, 'Unregistered'):
            self.request()

    def test_active_pinned_channel_and_non_codex_refusal(self):
        route = self.state.endpoints.pop(('demo', 'worker'))
        self.state.channels['demo', 'worker'] = {'active': True, 'route': {**route, 'channel': 'worker',
                                                                         'generation': 1}}
        requested = self.request(channel=True)
        self.assertEqual(self.claim()['id'], requested['id'])
        self.assertTrue(self.finish(requested['id']))
        self.state.channels['demo', 'worker']['route']['transport'] = 'agy'
        with self.assertRaisesRegex(ValueError, 'requires a Codex route'):
            self.request(channel=True)

    def test_malformed_reason_and_second_request_are_refused(self):
        for reason in ('', 'two\nlines', 'x' * 257, 42):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, 'printable'):
                self.request(reason=reason)
        first = self.request()
        with self.assertRaisesRegex(ValueError, 'already pending'):
            self.request()
        self.assertEqual(self.claim()['id'], first['id'])


class HookTests(unittest.TestCase):
    def test_fail_open_with_no_broker_invalid_payload_and_malformed_response(self):
        for payload, result in ((b'{', None), (event('invalid'), None), (event(name='Stop'), None),
                                (event(), TimeoutError()), (event(), {'id': THREAD, 'requested_at': []})):
            with self.subTest(payload=payload, result=result):
                output = io.StringIO()
                with patch.object(broker, 'request') as request:
                    if isinstance(result, Exception):
                        request.side_effect = result
                    else:
                        request.return_value = result
                    codex_stop_hook.run('fake.sock', io.BytesIO(payload), output)
                self.assertEqual(output.getvalue(), '')

    def test_real_broker_cli_and_one_hook_denial(self):
        with RunningBroker() as server:
            server.call('register', name='worker', project='demo', session=THREAD, transport='codex',
                        cwd=server.temp.name)
            cli = [sys.executable, str(SCRIPTS / 'bridge.py'), '--socket', server.path, 'stop', 'worker',
                   '--project', 'demo', '--reason', 'Pause']
            receipt = subprocess.run(cli, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(receipt.stdout)['state'], 'requested')
            decisions = []
            for thread in (OTHER, THREAD, THREAD):
                output = io.StringIO()
                codex_stop_hook.run(server.path, io.BytesIO(event(thread)), output)
                decisions.append(output.getvalue())
            self.assertEqual(decisions[::2], ['', ''])
            hook = json.loads(decisions[1])['hookSpecificOutput']
            self.assertEqual(hook['permissionDecision'], 'deny')
            self.assertIn('Operator reason: Pause', hook['permissionDecisionReason'])
            self.assertEqual(server.call('ping')['messages'], 0)

    def test_invalid_stop_arguments_cannot_trigger_broker_shutdown(self):
        args = bridge.build_parser().parse_args(['stop', '--project', 'demo'])
        with patch.object(broker, 'request') as request:
            with self.assertRaisesRegex(ValueError, 'needs NAME'):
                bridge.stop_command(args)
        request.assert_not_called()


class HookConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'hooks.json'
        env = patch.dict(os.environ, {'CODEX_HOME': folder.name})
        env.start()
        self.addCleanup(env.stop)

    def config(self, action, apply=False):
        return codex_hook.configure(action, apply, '/tmp/bridge test.sock', SCRIPTS / 'bridge.py')

    def test_install_and_uninstall_preserve_existing_bytes_and_mode(self):
        for data in (b' {}\n', b'{"description": "example"}\r\n', b'{"hooks": {}}',
                     b'{"hooks": {"Stop": []}}', b'{"hooks":{"PreToolUse":[]}}',
                     b'{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[]}]}}\n'):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                self.path.chmod(0o640)
                self.assertIn('insert', self.config('install'))
                self.assertEqual(self.path.read_bytes(), data)
                self.assertTrue(self.config('install', True)['applied'])
                self.assertTrue(self.config('status')['installed'])
                self.assertFalse(self.config('install', True)['changed'])
                self.assertIn('remove', self.config('uninstall'))
                self.assertTrue(self.config('uninstall', True)['applied'])
                self.assertEqual(self.path.read_bytes(), data)
                self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_new_file_is_created_only_on_apply_and_then_deleted(self):
        self.assertIn('create', self.config('install'))
        self.assertFalse(self.path.exists())
        self.assertTrue(self.config('install', True)['applied'])
        self.assertIn('delete', self.config('uninstall'))
        self.assertTrue(self.path.exists())
        self.assertTrue(self.config('uninstall', True)['applied'])
        self.assertFalse(self.path.exists())

    def test_edited_managed_group_is_refused_without_removing_other_handlers(self):
        self.config('install', True)
        config = json.loads(self.path.read_bytes())
        config['hooks']['PreToolUse'][0]['hooks'].append({'type': 'command', 'command': 'echo preserve'})
        changed = json.dumps(config)
        self.path.write_text(changed)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.config('uninstall', True)
        self.assertEqual(self.path.read_text(), changed)
