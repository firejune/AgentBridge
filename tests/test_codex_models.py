"""Codex #13 contracts; every provider/owner/broker endpoint is isolated."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import codex_live
import codex_new
import models
import new_session_policy
import sessions
from test_codex_new import ModelDesktop

IPC_CLIENT = codex_live.Client
SOL = 'gpt-6.1-sol'
ASTRA = 'gpt-6-astra'


class CodexModelsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.sid = str(uuid.uuid4())
        self.route = {'name': 'worker', 'project': 'demo', 'transport': 'codex', 'session': self.sid}
        self.policy = models.load_policy(self.state)
        self.catalog_calls = []
        # A missing stub is a failing test, never permission to launch a live CLI
        # or connect to the default Desktop socket.
        for target in ((sessions, 'CodexClient'), (codex_live, 'Client')):
            guard = patch.object(*target, side_effect=AssertionError('unstubbed live provider access'))
            guard.start()
            self.addCleanup(guard.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(codex_new.MODEL_ENV, None)

    def owner(self, desktop):
        return patch.object(codex_live, 'Client',
                            side_effect=lambda **kw: IPC_CLIENT(desktop.path, **kw))

    def catalog(self, **kwargs):
        self.assertEqual(kwargs['mode'], 'saved')
        self.assertTrue(kwargs['experimental'])
        calls = self.catalog_calls

        class Catalog:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def call(self, method, params):
                if method != 'model/list':
                    raise AssertionError('catalog must not create a thread or turn')
                calls.append((method, params))
                return {'data': [{'model': SOL}, {'model': 'another-sol'}]}
        return Catalog()

    def cli(self, *args):
        out = io.StringIO()
        with patch('sys.stdout', out):
            code = bridge.main([*args, '--state-dir', str(self.state)])
        return code, json.loads(out.getvalue())

    def test_read_is_fresh_before_any_turn_and_never_mutates(self):
        with ModelDesktop(model=ASTRA) as desktop, self.owner(desktop):
            first = models.read(self.route)
            desktop.model = SOL
            second = models.read(self.route)
        self.assertEqual((first['model'], second['model']), (ASTRA, SOL))
        self.assertEqual((first['provider'], first['session']), ('codex', self.sid))
        self.assertIn('next turn', first['source'])
        settings = [x for x in desktop.calls if x['method'] == 'thread-follower-read-model-settings']
        self.assertEqual(len(settings), 2)
        self.assertTrue(all(x['targetClientId'] == desktop.owner for x in settings))
        self.assertTrue(all(x['params'] == {'conversationId': self.sid} for x in settings))
        self.assertTrue(all(x['version'] == 1 for x in settings))
        self.assertFalse(any('update' in x['method'] or 'start-turn' in x['method'] for x in desktop.calls))

    def test_missing_owner_and_missing_resumed_settings_are_unknown(self):
        with ModelDesktop(behavior='no-owner') as desktop, self.owner(desktop):
            result = models.read(self.route)
        self.assertEqual(result['model'], 'unknown')
        self.assertIn('no owner', result['reason'])
        for settings in ({}, {'model': SOL, 'resumeState': 'not-resumed'},
                         {'model': None, 'resumeState': 'resumed'}):
            with self.subTest(settings=settings), patch.object(codex_live, 'Client') as factory:
                factory.return_value.__enter__.return_value.call.return_value = {
                    'resultType': 'success', 'result': {'settings': settings}}
                result = models.read(self.route)
            self.assertEqual(result['model'], 'unknown')
            self.assertIn('resumed model settings', result['reason'])

    def test_read_rejects_invalid_route_uuid_without_contacting_owner(self):
        with patch.object(codex_live, 'Client') as factory:
            result = models.read({**self.route, 'session': 'not-a-uuid'})
        self.assertEqual(result['model'], 'unknown')
        factory.assert_not_called()

    def test_switch_uses_exact_catalog_owner_model_only_and_readback(self):
        with ModelDesktop(model=ASTRA) as desktop, self.owner(desktop), \
                patch.object(sessions, 'CodexClient', side_effect=self.catalog):
            result = models.switch(self.route, SOL, self.policy, timeout=1)
        self.assertEqual((result['provider'], result['model'], result['previous_model'], result['changed']),
                         ('codex', SOL, ASTRA, True))
        updates = [x for x in desktop.calls if x['method'] == 'thread-follower-update-thread-settings']
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['params'], {'conversationId': self.sid, 'threadSettings': {'model': SOL}})
        self.assertEqual(updates[0]['targetClientId'], desktop.owner)
        self.assertEqual(updates[0]['version'], 2)
        self.assertEqual(sum(x['method'] == 'thread-follower-read-model-settings' for x in desktop.calls), 2)
        self.assertFalse(any(x['method'].endswith(('start-turn', 'steer-turn')) for x in desktop.calls))

    def test_current_model_is_noop_and_unadvertised_is_refused_before_owner(self):
        with ModelDesktop(model=SOL) as desktop, self.owner(desktop), \
                patch.object(sessions, 'CodexClient', side_effect=self.catalog):
            self.assertFalse(models.switch(self.route, SOL, self.policy)['changed'])
        self.assertFalse(any('update' in x['method'] for x in desktop.calls))
        with patch.object(codex_live, 'Client') as owner, \
                patch.object(sessions, 'CodexClient', side_effect=self.catalog), \
                self.assertRaises(models.SwitchError) as caught:
            models.switch(self.route, 'not-advertised', self.policy)
        self.assertFalse(caught.exception.uncertain)
        owner.assert_not_called()

    def test_lost_update_and_wrong_readback_are_uncertain_with_one_attempt(self):
        for options in ({'drop_update': True}, {'keep_model': True}, {'reject_update': True}):
            with self.subTest(options=options), ModelDesktop(model=ASTRA, **options) as desktop, \
                    self.owner(desktop), patch.object(sessions, 'CodexClient', side_effect=self.catalog), \
                    self.assertRaises(models.SwitchError) as caught:
                models.switch(self.route, SOL, self.policy, timeout=.1)
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(sum(x['method'] == 'thread-follower-update-thread-settings' for x in desktop.calls), 1)
            self.assertEqual(sum(x['method'] == 'initialize' for x in desktop.calls), 1)

    def test_exact_names_and_denylist_refuse_before_provider_access(self):
        self.assertEqual(models.adapter_names(self.route, ASTRA, 10), [ASTRA])
        with patch.object(codex_new, 'set_model') as switch:
            for model in (ASTRA, 'GPT-6-ASTRA'):
                with self.assertRaisesRegex(ValueError, 'denylist'):
                    models.switch(self.route, model, self.policy)
        switch.assert_not_called()

    def test_codex_switch_accepts_neutral_policy_and_relay_without_using_relay(self):
        relay = object()  # Any relay operation would fail; Codex switches locally.
        result = {'session': self.sid, 'model': SOL, 'changed': True}
        with patch.object(codex_new, 'set_model', return_value=result) as setter:
            out = models.switch(self.route, SOL, self.policy, timeout=2, relay=relay)
        self.assertEqual(out, {'provider': 'codex', **result})
        setter.assert_called_once_with(self.sid, SOL, timeout=2)

    def test_shared_loader_preserves_command_post_without_losing_codex_policy(self):
        path = self.state / 'policy.json'
        contents = json.dumps({'command_post': {'claude': 'hq'},
                               'defaults': {'codex': 'another-sol'}, 'deny': {'codex': [ASTRA]}})
        path.write_text(contents)
        policy = new_session_policy.load_policy(self.state)
        self.assertIs(models.load_policy, new_session_policy.load_policy)
        self.assertEqual(models.load_policy(self.state), policy)
        self.assertEqual(policy['command_post'], {'claude': 'hq'})
        self.assertEqual(codex_new.resolve_model(None, {}, policy), ('another-sol', 'policy'))
        with self.assertRaisesRegex(ValueError, 'denylist'):
            models.switch(self.route, ASTRA, policy, relay=object())
        self.assertEqual(path.read_text(), contents)

    def test_shared_loader_defaults_and_validates_command_post_for_both_callers(self):
        self.assertEqual(self.policy['command_post'], {})
        path = self.state / 'policy.json'
        for invalid in (None, [], 'hq', {'claude': ''}, {'claude': ' '}, {'claude': 1}):
            path.write_text(json.dumps({'command_post': invalid}))
            for loader in (models.load_policy, new_session_policy.load_policy):
                with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, 'command_post'):
                    loader(self.state)

    def test_cli_model_and_routes_read_flag_denied_and_skip_on_request(self):
        def broker_request(socket, fields, **kwargs):
            self.assertEqual(fields['op'], 'routes')
            return [dict(self.route)]
        with ModelDesktop(model=ASTRA) as desktop, self.owner(desktop), \
                patch.object(bridge.broker, 'request', side_effect=broker_request):
            code, out = self.cli('model', 'worker', '--project', 'demo')
            self.assertEqual((code, out['name'], out['model'], out['denied']), (0, 'worker', ASTRA, True))
            code, out = self.cli('routes', '--project', 'demo')
            self.assertEqual((code, out[0]['model']['model'], out[0]['model']['denied']), (0, ASTRA, True))
            desktop.model = SOL
            code, out = self.cli('routes', '--project', 'demo')
            self.assertEqual((code, out[0]['model']['model'], out[0]['model']['denied']), (0, SOL, False))
            count = len(desktop.calls)
            code, out = self.cli('routes', '--project', 'demo', '--no-model')
            self.assertEqual(code, 0)
            self.assertNotIn('model', out[0])
            self.assertEqual(len(desktop.calls), count)

    def test_cli_switch_keeps_uncertainty_and_denial_before_dispatch(self):
        with patch.object(bridge.broker, 'request', return_value=[self.route]), \
                patch.object(codex_new, 'set_model', side_effect=codex_new.ModelSwitchError('lost', True)) as switch:
            code, out = self.cli('model', 'worker', '--project', 'demo', '--set', SOL)
            self.assertEqual((code, out['uncertain'], out['error']), (1, True, 'lost'))
            switch.assert_called_once_with(self.sid, SOL, timeout=30)
            switch.reset_mock()
            code, out = self.cli('model', 'worker', '--project', 'demo', '--set', ASTRA)
            self.assertEqual(code, 1)
            self.assertIn('denylist', out['error'])
            switch.assert_not_called()

    def test_new_cli_refuses_denied_flag_and_env_for_open_and_register(self):
        for command in (['open', '--provider', 'codex'],
                        ['register', 'worker', '--project', 'demo', '--transport', 'codex', '--new']):
            for explicit in (True, False):
                env = {} if explicit else {codex_new.MODEL_ENV: ASTRA}
                extra = ['--model', ASTRA] if explicit else []
                with self.subTest(command=command[0], explicit=explicit), patch.dict(os.environ, env), \
                        patch.object(codex_new, 'open_codex') as create:
                    code, out = self.cli(*command, '--cwd', str(self.state), *extra)
                self.assertEqual(code, 1)
                self.assertIn('denylist', out['error'])
                create.assert_not_called()

    def test_one_default_and_policy_source_with_flag_env_policy_sol_precedence(self):
        self.assertIs(models.default_model, new_session_policy.default_model)
        self.assertIs(models.load_policy, new_session_policy.load_policy)
        policy = {**self.policy, 'defaults': {'codex': 'policy-sol'}}
        for model, env, expected in (
                ('flag-sol', {codex_new.MODEL_ENV: 'env-sol'}, ('flag-sol', '--model')),
                (None, {codex_new.MODEL_ENV: 'env-sol'}, ('env-sol', codex_new.MODEL_ENV)),
                (None, {}, ('policy-sol', 'policy'))):
            before = copy.deepcopy(policy)
            self.assertEqual(codex_new.resolve_model(model, env, policy), expected)
            self.assertEqual(models.default_model('codex', model, policy, env), expected)
            self.assertEqual(policy, before)
        self.assertEqual(codex_new.resolve_model(None, {}, self.policy), (SOL, 'default'))
        with self.assertRaisesRegex(codex_new.CreationError, 'set but empty'):
            codex_new.resolve_model(None, {codex_new.MODEL_ENV: ' '}, policy)

    def test_policy_edits_remain_human_controlled_not_merged_with_seed(self):
        path = self.state / 'policy.json'
        text = json.dumps({'defaults': {'codex': ASTRA}, 'deny': {}})
        path.write_text(text)
        policy = models.load_policy(self.state)
        self.assertEqual(codex_new.resolve_model(None, {}, policy), (ASTRA, 'policy'))
        self.assertEqual(path.read_text(), text)

    def test_new_cli_forwards_resolved_flag_env_policy_and_sol_with_source(self):
        for registering in (False, True):
            for explicit, env_model, default, expected, source in (
                    ('flag-sol', 'env-sol', 'policy-sol', 'flag-sol', '--model'),
                    (None, 'env-sol', 'policy-sol', 'env-sol', codex_new.MODEL_ENV),
                    (None, None, 'policy-sol', 'policy-sol', 'policy'),
                    (None, None, None, SOL, 'default')):
                with self.subTest(register=registering, source=source):
                    (self.state / 'policy.json').write_text(json.dumps({
                        'defaults': {'codex': default} if default else {}, 'deny': {'codex': [ASTRA]}}))
                    command = (['register', 'worker', '--project', 'demo', '--transport', 'codex', '--new']
                               if registering else ['open', '--provider', 'codex'])
                    extra = ['--model', explicit] if explicit else []
                    env = {codex_new.MODEL_ENV: env_model} if env_model else {}
                    created = {'created': True, 'adopted': True, 'model': expected, 'model_source': '--model',
                               'session': {'id': self.sid, 'cwd': str(self.state)}}
                    with patch.dict(os.environ, env), \
                            patch.object(codex_new, 'open_codex', return_value=created) as create, \
                            patch.object(bridge.broker, 'request', return_value=dict(self.route)) as broker:
                        code, out = self.cli(*command, '--cwd', str(self.state), *extra)
                    self.assertEqual(code, 0, out)
                    create.assert_called_once()
                    self.assertEqual(create.call_args.kwargs['model'], expected)
                    result = out['discovery'] if registering else out
                    self.assertEqual((result['model'], result['model_source']), (expected, source))
                    if registering:
                        self.assertTrue(out['registered'])
                        self.assertEqual(broker.call_args.args[1]['session'], self.sid)
                        self.assertNotIn('model_source', broker.call_args.args[1])
                    else:
                        broker.assert_not_called()

    def test_denied_state_policy_default_stops_open_and_register_before_creation(self):
        (self.state / 'policy.json').write_text(json.dumps({
            'defaults': {'codex': ASTRA}, 'deny': {'codex': [ASTRA]}}))
        for command in (['open', '--provider', 'codex'],
                        ['register', 'worker', '--project', 'demo', '--transport', 'codex', '--new']):
            with self.subTest(command=command[0]), patch.object(codex_new, 'open_codex') as create, \
                    patch.object(bridge.broker, 'request') as broker:
                code, out = self.cli(*command, '--cwd', str(self.state))
            self.assertEqual(code, 1)
            self.assertIn('denylist', out['error'])
            create.assert_not_called()
            broker.assert_not_called()

    def test_alias_refuses_seed_and_custom_denylists_before_dispatch(self):
        for model, contents in ((ASTRA, None), ('another-sol', {'deny': {'codex': ['another-sol']}})):
            if contents is not None:
                (self.state / 'policy.json').write_text(json.dumps(contents))
            with self.subTest(model=model), patch.object(codex_new, 'set_model') as switch, \
                    patch.object(bridge.broker, 'request') as broker:
                code, out = self.cli('codex-model', self.sid, '--model', model)
            self.assertEqual(code, 1)
            self.assertIn('denylist', out['error'])
            switch.assert_not_called()
            broker.assert_not_called()

    def test_alias_infers_state_from_socket_and_malformed_policy_fails_closed(self):
        socket = self.state / 'isolated.sock'
        state = socket.with_suffix('.state')
        state.mkdir()
        for contents in (json.dumps({'deny': {'codex': [SOL]}}), '{'):
            (state / 'policy.json').write_text(contents)
            out = io.StringIO()
            with patch('sys.stdout', out), patch.object(codex_new, 'set_model') as switch:
                code = bridge.main(['--socket', str(socket), 'codex-model', self.sid, '--model', SOL])
            self.assertEqual(code, 1)
            self.assertIn('error', json.loads(out.getvalue()))
            switch.assert_not_called()

    def test_alias_obeys_human_policy_and_preserves_uncertainty_without_retry(self):
        (self.state / 'policy.json').write_text(json.dumps({'deny': {}}))
        ok = {'session': self.sid, 'model': ASTRA, 'changed': True}
        with patch.object(codex_new, 'set_model', return_value=ok) as switch:
            code, out = self.cli('codex-model', self.sid, '--model', ASTRA)
        self.assertEqual((code, out), (0, {'provider': 'codex', **ok}))
        switch.assert_called_once_with(self.sid, ASTRA, timeout=10)
        with ModelDesktop(model=ASTRA, drop_update=True) as desktop, self.owner(desktop), \
                patch.object(sessions, 'CodexClient', side_effect=self.catalog):
            code, out = self.cli('codex-model', self.sid, '--model', SOL, '--timeout', '.1')
        self.assertEqual((code, out['uncertain'], out['session'], out['model']), (1, True, self.sid, SOL))
        self.assertEqual(sum(x['method'] == 'thread-follower-update-thread-settings' for x in desktop.calls), 1)

    def test_sessions_annotates_next_turn_settings_fresh_and_skips_on_request(self):
        def discovered(*args):
            return {'providers': {'codex': {'sessions': [{'provider': 'codex', 'id': self.sid,
                                                         'status': 'active'}]}}, 'errors': {}}
        with ModelDesktop(model=ASTRA) as desktop, self.owner(desktop), \
                patch.object(sessions, 'discover', side_effect=discovered), \
                patch.object(bridge.broker, 'request') as broker:
            code, out = self.cli('sessions', '--provider', 'codex')
            row = out['providers']['codex']['sessions'][0]
            self.assertEqual((code, row['status'], row['model']['model'], row['model']['denied']),
                             (0, 'active', ASTRA, True))
            self.assertIn('next turn', row['model']['source'])
            desktop.model = SOL
            code, out = self.cli('sessions', '--provider', 'codex')
            row = out['providers']['codex']['sessions'][0]
            self.assertEqual((code, row['model']['model'], row['model']['denied']), (0, SOL, False))
            count = len(desktop.calls)
            code, out = self.cli('sessions', '--provider', 'codex', '--no-model')
            self.assertEqual(code, 0)
            self.assertNotIn('model', out['providers']['codex']['sessions'][0])
            self.assertEqual(len(desktop.calls), count)
            broker.assert_not_called()

    def test_sessions_owner_failure_is_unknown_without_losing_discovery_rows(self):
        discovered = {'providers': {'codex': {'sessions': [{'provider': 'codex', 'id': self.sid}]}},
                      'errors': {}}
        with patch.object(sessions, 'discover', return_value=discovered), \
                patch.object(codex_live, 'Client', side_effect=codex_live.DeliveryError('owner unavailable')):
            code, out = self.cli('sessions', '--provider', 'codex')
        row = out['providers']['codex']['sessions'][0]
        self.assertEqual((code, row['id'], row['model']['model'], row['model']['reason']),
                         (0, self.sid, 'unknown', 'owner unavailable'))


if __name__ == '__main__':
    unittest.main()
