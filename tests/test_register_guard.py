"""Name ownership guards; all endpoints and creations are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
import broker
import codex_new
import route_store
from support import RunningBroker


class RegistrationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-register-guard-')
        self.addCleanup(self.temp.cleanup)
        self.store = route_store.RouteStore(Path(self.temp.name) / 'state')
        self.addCleanup(self.store.close)
        self.state = broker.State(store=self.store)
        self.args = dict(op='register', project='demo', name='worker', transport='realtime',
                         cwd=self.temp.name, session=str(uuid.uuid4()), executable='/synthetic/tool')
        self.original = self.state.action(self.args)

    def test_exact_binding_and_omitted_executable_are_no_write_idempotent(self):
        with patch.object(self.store, 'save', side_effect=AssertionError('No repeat write')):
            self.assertEqual(self.state.action(self.args), self.original)
            self.assertEqual(self.state.action(dict(self.args, executable=None)), self.original)
            omitted = dict(self.args); omitted.pop('executable')
            self.assertEqual(self.state.action(omitted), self.original)

    def test_competing_identity_or_executable_is_refused_before_write(self):
        other = Path(self.temp.name) / 'other'; other.mkdir()
        before = self.store.path.read_bytes()
        for change in ({'session': str(uuid.uuid4())}, {'transport': 'codex'},
                       {'cwd': str(other)}, {'executable': '/synthetic/other'}):
            with self.subTest(change=tuple(change)), patch.object(self.store, 'save', side_effect=AssertionError('No conflicting write')):
                with self.assertRaisesRegex(ValueError, 'use a different name') as error:
                    self.state.action(dict(self.args, **change))
                self.assertEqual(str(error.exception), broker.REGISTER_CONFLICT_ERROR)
                self.assertEqual(self.state.route('demo', 'worker'), self.original)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_project_namespace_and_explicit_codex_remap_remain_independent(self):
        other = self.state.action(dict(self.args, project='other', session=str(uuid.uuid4())))
        self.assertNotEqual(other['session'], self.original['session'])
        codex = self.state.action(dict(self.args, name='codex-worker', transport='codex'))
        replacement = str(uuid.uuid4())
        mapped = self.state.action(dict(op='remap', project='demo', name='codex-worker',
                                       expected_route=codex, session=replacement))
        self.assertEqual(mapped, dict(codex, session=replacement))
        self.assertEqual(self.state.route('demo', 'worker'), self.original)

    def test_refused_competitor_preserves_pinned_channel_and_next_send_after_restart(self):
        self.state.action(dict(op='register', project='demo', name='sender', cwd=self.temp.name))
        pinned = self.state.action(dict(op='channel-bind', project='demo', name='work', owner='worker', description='pinned'))
        with self.assertRaises(ValueError):
            self.state.action(dict(self.args, session=str(uuid.uuid4())))
        message = self.state.action(dict(op='send', project='demo', sender='sender', channel='work', body='original owner', key='first'))
        self.assertEqual(message['route'], pinned['route'])
        restored = broker.State(store=self.store)
        self.assertEqual(restored.route('demo', 'worker'), self.original)
        next_message = restored.action(dict(op='send', project='demo', sender='sender', recipient='worker', body='still original', key='second'))
        self.assertEqual(next_message['route'], self.original)
        self.assertEqual(restored.channels['demo', 'work'], pinned)

    def test_concurrent_different_sessions_have_one_winner_and_normal_send_uses_it(self):
        with RunningBroker() as running:
            args = dict(project='demo', name='worker', transport='realtime', cwd=running.temp.name)
            def register(sid):
                try:
                    return running.call('register', session=sid, **args)
                except ValueError as error:
                    return str(error)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(register, [str(uuid.uuid4()), str(uuid.uuid4())]))
            winners = [value for value in results if isinstance(value, dict)]
            self.assertEqual(len(winners), 1)
            self.assertEqual([value for value in results if isinstance(value, str)], [broker.REGISTER_CONFLICT_ERROR])
            running.call('register', project='demo', name='sender', cwd=running.temp.name)
            message = running.call('send', project='demo', sender='sender', recipient='worker', body='winner', key='winner')
            self.assertEqual(message['route'], winners[0])
            running.restart()
            self.assertEqual(running.call('routes', project='demo')[0], winners[0])


class RegistrationCreationPreflightTests(unittest.TestCase):
    def test_known_name_stops_all_creation_entry_paths(self):
        with RunningBroker() as running:
            running.call('register', project='demo', name='worker', cwd=running.temp.name)
            commands = [
                ['register', 'worker', '--project', 'demo', '--transport', provider, '--new', '--cwd', running.temp.name]
                for provider in ('agy', 'claude-peer', 'codex')]
            commands.append(['open', '--provider', 'codex', '--register', 'worker', '--project', 'demo', '--cwd', running.temp.name])
            for command in commands:
                with self.subTest(command=command[:2]), patch.object(bridge.sessions, 'open_session') as opened, patch.object(bridge, 'new_session_policy', return_value='synthetic'), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(bridge.main(['--socket', running.path, *command]), 1)
                    result = json.loads(output.getvalue())
                    self.assertEqual(result['error'], broker.REGISTER_CONFLICT_ERROR)
                    self.assertFalse(result.get('created', False))
                    self.assertFalse(result.get('uncertain', False))
                    opened.assert_not_called()

    def test_plain_same_binding_registration_has_no_creation_preflight(self):
        sid = str(uuid.uuid4())
        with patch.object(bridge.broker, 'request', return_value={}) as request, redirect_stdout(io.StringIO()):
            self.assertEqual(bridge.main(['register', 'worker', '--project', 'demo', '--transport', 'realtime', '--cwd', '/tmp', '--session', sid]), 0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1]['op'], 'register')

    def test_codex_atomic_conflict_and_ambiguous_write_are_distinct(self):
        for failure, uncertain in ((ValueError(broker.REGISTER_CONFLICT_ERROR), False), (OSError('lost reply'), True)):
            with self.subTest(uncertain=uncertain):
                result = codex_new.open_result()
                result.update(created=True, adopted=True, stage='complete', session=dict(result['session'], id=str(uuid.uuid4()), cwd='/tmp'))
                calls = []
                def request(path, args):
                    calls.append(args['op'])
                    if args['op'] == 'routes':
                        return []
                    raise failure
                with patch.object(bridge.broker, 'request', side_effect=request), patch.object(bridge.sessions, 'open_session', return_value=result) as opened, patch.object(bridge, 'new_session_policy', return_value='synthetic'), redirect_stdout(io.StringIO()) as output:
                    code = bridge.main(['open', '--provider', 'codex', '--cwd', '/tmp', '--register', 'worker', '--project', 'demo'])
                value = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(value['uncertain'], uncertain)
                self.assertTrue(value['created'])
                self.assertEqual(value['session']['id'], result['session']['id'])
                self.assertFalse(value['registered'])
                self.assertEqual(calls, ['routes', 'register'])
                self.assertEqual(opened.call_count, 1)
                if not uncertain:
                    self.assertEqual(value['registration_error'], broker.REGISTER_CONFLICT_ERROR)

    def test_unreadable_preflight_creates_no_session(self):
        with patch.object(bridge.broker, 'request', side_effect=OSError('private socket')), patch.object(bridge.sessions, 'open_session') as opened, patch.object(bridge, 'new_session_policy', return_value='synthetic'), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bridge.main(['open', '--provider', 'codex', '--cwd', '/tmp', '--register', 'worker', '--project', 'demo']), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result['created'])
        self.assertFalse(result['uncertain'])
        self.assertNotIn('private socket', result['error'])
        opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
