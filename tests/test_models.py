import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import agy_live
import bridge
import codex_new
import models
import sessions
from support import RunningBroker

SID = '3dcb594b-00bb-4bde-bb3e-afb322abb88a'
CATALOG = [
    {'label': 'Gemini 3.8 Flash (Low)', 'modelId': 'gemini-3.8-flash-low', 'modelOrAlias': {'model': 'MODEL_PLACEHOLDER_M320'}},
    {'label': 'Gemini 3.8 Flash (Medium)', 'modelId': 'gemini-3.8-flash-medium', 'modelOrAlias': {'model': 'MODEL_PLACEHOLDER_M319'}},
]


def run(argv):
    out = io.StringIO()
    with patch('sys.stdout', out):
        code = bridge.main(argv)
    return code, json.loads(out.getvalue())


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_applies_only_without_a_file(self):
        policy = models.load_policy(self.dir)
        self.assertEqual((policy['exists'], policy['deny']), (False, {'codex': ['gpt-6-astra']}))
        (self.dir / 'policy.json').write_text('{"deny": {}}')
        self.assertEqual(models.load_policy(self.dir)['deny'], {})

    def test_malformed_policy_is_an_error_not_an_empty_list(self):
        for text in ('{', '[]', '{"deny": {"codex": "gpt-6-astra"}}', '{"defaults": {"agy": ""}}'):
            (self.dir / 'policy.json').write_text(text)
            with self.assertRaises(ValueError):
                models.load_policy(self.dir)

    def test_denylist_matches_any_name_ignoring_case(self):
        policy = models.load_policy(self.dir)
        self.assertEqual(models.denied(policy, 'codex', 'x', 'GPT-6-Astra'), 'GPT-6-Astra')
        self.assertIsNone(models.denied(policy, 'agy', 'gpt-6-astra'))
        with self.assertRaisesRegex(ValueError, 'denylist in the built-in seed'):
            models.require_allowed(policy, 'codex', 'gpt-6-astra')

    def test_default_precedence(self):
        policy = {'defaults': {'agy': 'from-policy'}, 'deny': {}}
        self.assertEqual(models.default_model('agy', 'flag', policy, {'AGENT_BRIDGE_AGY_MODEL': 'env'}), ('flag', '--model'))
        self.assertEqual(models.default_model('agy', None, policy, {'AGENT_BRIDGE_AGY_MODEL': 'env'}),
                         ('env', 'AGENT_BRIDGE_AGY_MODEL'))
        self.assertEqual(models.default_model('agy', None, policy, {}), ('from-policy', 'policy'))
        self.assertEqual(models.default_model('claude', None, policy, {}), (None, 'provider default'))
        with self.assertRaisesRegex(ValueError, 'set but empty'):
            models.default_model('agy', None, policy, {'AGENT_BRIDGE_AGY_MODEL': ' '})


class NewSessionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_codex_env_default_on_the_denylist_is_refused_before_creation(self):
        with patch.dict('os.environ', {'AGENT_BRIDGE_CODEX_MODEL': 'gpt-6-astra'}), \
                patch.object(codex_new, 'open_codex') as opened:
            code, out = run(['open', '--provider', 'codex', '--cwd', self.state, '--state-dir', self.state])
        self.assertEqual(code, 1)
        self.assertIn('denylist', out['error'])
        opened.assert_not_called()

    def test_codex_new_model_on_the_denylist_is_refused_at_register(self):
        with patch.object(codex_new, 'open_codex') as opened:
            code, out = run(['register', 'w', '--project', 'p', '--transport', 'codex', '--new', '--cwd', self.state,
                             '--model', 'gpt-6-astra', '--state-dir', self.state])
        self.assertEqual(code, 1)
        self.assertIn('denylist', out['error'])
        opened.assert_not_called()

    def test_claude_new_with_a_model_is_a_named_gap(self):
        for env, extra in (({}, ['--model', 'claude-sonnet-5-5']), ({'AGENT_BRIDGE_CLAUDE_MODEL': 'claude-sonnet-5-5'}, [])):
            with patch.dict('os.environ', env), patch.object(sessions, 'open_claude') as opened:
                code, out = run(['open', '--provider', 'claude', '--cwd', self.state, '--state-dir', self.state, *extra])
            self.assertEqual((code, out.get('unsupported')), (1, True))
            self.assertIn('cannot be given a model', out['error'])
            opened.assert_not_called()

    def test_agy_new_refuses_a_denied_alias_and_passes_the_resolved_id(self):
        Path(self.state, 'policy.json').write_text('{"deny": {"agy": ["Gemini 3.8 Flash (Medium)"]}}')
        server = {'url': 'http://127.0.0.1:1', 'csrf': 'x'}
        found = {'model_enum': 'MODEL_PLACEHOLDER_M319', 'model_id': 'gemini-3.8-flash-medium', 'label': 'Gemini 3.8 Flash (Medium)'}
        with patch.object(agy_live, 'discover_desktop_server', return_value=server), \
                patch.object(agy_live, 'resolve_model', return_value=found), \
                patch.object(agy_live, 'open_agy') as opened:
            code, out = run(['open', '--provider', 'agy', '--cwd', self.state, '--model', 'MODEL_PLACEHOLDER_M319',
                             '--state-dir', self.state])
            self.assertEqual(code, 1)
            self.assertIn('denylist', out['error'])
            opened.assert_not_called()
            low = {'model_enum': 'MODEL_PLACEHOLDER_M320', 'model_id': 'gemini-3.8-flash-low', 'label': 'Gemini 3.8 Flash (Low)'}
            with patch.object(agy_live, 'resolve_model', return_value=low), \
                    patch.dict('os.environ', {'AGENT_BRIDGE_AGY_MODEL': 'flash-low'}):
                opened.return_value = {'created': True}
                code, out = run(['open', '--provider', 'agy', '--cwd', self.state, '--state-dir', self.state])
            self.assertEqual(code, 0)
            self.assertEqual(opened.call_args.kwargs['model'], 'gemini-3.8-flash-low')


class ModelCommandTest(unittest.TestCase):
    def call(self, broker, *argv):
        return run(['--socket', broker.path, *argv, '--state-dir', broker.state_dir])

    def register(self, broker, name, transport, sid=SID):
        broker.call('register', project='demo', name=name, cwd=broker.temp.name, transport=transport,
                    session=None if transport == 'realtime' else sid)

    def test_read_switch_and_named_gaps_by_participant_name(self):
        reading = {'session': SID, 'status': 'idle', 'model': 'gemini-3.8-flash-low',
                   'model_enum': 'MODEL_PLACEHOLDER_M320', 'label': 'Gemini 3.8 Flash (Low)'}
        switched = {'session': SID, 'model': 'gemini-3.8-flash-medium', 'changed': True}
        with RunningBroker() as broker:
            self.register(broker, 'agy-w', 'agy')
            self.register(broker, 'ide', 'claude-peer', str(uuid.uuid4()))
            self.register(broker, 'cli', 'realtime')
            self.register(broker, 'nx', 'codex', str(uuid.uuid4()))
            with patch.object(agy_live, 'read_model', return_value=reading):
                code, out = self.call(broker, 'model', 'agy-w', '--project', 'demo')
            self.assertEqual((code, out['model'], out['denied'], out['provider']), (0, 'gemini-3.8-flash-low', False, 'agy'))
            names = ['flash-medium', 'gemini-3.8-flash-medium', 'MODEL_PLACEHOLDER_M319', 'Gemini 3.8 Flash (Medium)']
            with patch.dict(models.ADAPTERS['agy'], {'names': lambda *a: names}), \
                    patch.object(agy_live, 'set_model', return_value=switched) as setter:
                code, out = self.call(broker, 'model', 'agy-w', '--project', 'demo', '--set', 'flash-medium')
            self.assertEqual((code, out['changed']), (0, True))
            self.assertEqual(setter.call_args.args[:2], (SID, 'flash-medium'))
            with patch.object(models, 'claude_host', return_value='claude-vscode'):
                code, out = self.call(broker, 'model', 'ide', '--project', 'demo', '--set', 'claude-sonnet-5-5')
            self.assertEqual((code, out['unsupported']), (1, True))
            self.assertIn('claude (claude-vscode)', out['error'])
            code, out = self.call(broker, 'model', 'cli', '--project', 'demo')
            self.assertEqual((code, out['model']), (0, 'unknown'))
            self.assertIn('realtime', out['reason'])
            with patch.object(models.codex_live, 'Client', side_effect=ValueError('Codex owner unavailable')):
                code, out = self.call(broker, 'model', 'nx', '--project', 'demo')
            self.assertEqual(out['model'], 'unknown')
            self.assertEqual(out['reason'], 'Codex owner unavailable')
            code, out = self.call(broker, 'model', 'missing', '--project', 'demo')
            self.assertEqual(code, 1)

    def test_switch_to_a_denied_model_never_reaches_the_provider(self):
        with RunningBroker() as broker:
            self.register(broker, 'agy-w', 'agy')
            Path(broker.state_dir).mkdir(parents=True, exist_ok=True)
            Path(broker.state_dir, 'policy.json').write_text('{"deny": {"agy": ["gemini-3.8-flash-medium"]}}')
            names = ['MODEL_PLACEHOLDER_M319', 'gemini-3.8-flash-medium', 'MODEL_PLACEHOLDER_M319', None]
            with patch.dict(models.ADAPTERS['agy'], {'names': lambda *a: names}), \
                    patch.object(agy_live, 'set_model') as setter:
                code, out = self.call(broker, 'model', 'agy-w', '--project', 'demo', '--set', 'MODEL_PLACEHOLDER_M319')
            self.assertEqual(code, 1)
            self.assertIn('denylist', out['error'])
            setter.assert_not_called()

    def test_uncertain_switch_is_reported_and_exits_nonzero(self):
        with RunningBroker() as broker:
            self.register(broker, 'agy-w', 'agy')
            error = agy_live.ModelSwitchError('lost', uncertain=True)
            with patch.dict(models.ADAPTERS['agy'], {'names': lambda *a: ['x']}), \
                    patch.object(agy_live, 'set_model', side_effect=error):
                code, out = self.call(broker, 'model', 'agy-w', '--project', 'demo', '--set', 'x')
            self.assertEqual((code, out['uncertain'], out['error']), (1, True, 'lost'))

    def test_routes_show_the_model_and_flag_a_denied_session(self):
        with RunningBroker() as broker:
            self.register(broker, 'agy-w', 'agy')
            Path(broker.state_dir).mkdir(parents=True, exist_ok=True)
            Path(broker.state_dir, 'policy.json').write_text('{"deny": {"agy": ["gemini-3.8-flash-low"]}}')
            reading = {'session': SID, 'model': 'gemini-3.8-flash-low', 'model_enum': 'MODEL_PLACEHOLDER_M320'}
            with patch.object(agy_live, 'read_model', return_value=reading):
                code, out = self.call(broker, 'routes', '--project', 'demo')
            self.assertEqual((code, out[0]['model']['model'], out[0]['model']['denied']), (0, 'gemini-3.8-flash-low', True))
            with patch.object(agy_live, 'read_model', side_effect=agy_live.DeliveryError('gone')):
                code, out = self.call(broker, 'routes', '--project', 'demo')
            self.assertEqual((out[0]['model']['model'], out[0]['model']['reason']), ('unknown', 'gone'))
            with patch.object(agy_live, 'read_model') as reader:
                code, out = self.call(broker, 'routes', '--project', 'demo', '--no-model')
            reader.assert_not_called()
            self.assertNotIn('model', out[0])


class FakeAgy:
    """Stub language_server: steps grow as invocations run."""

    def __init__(self, status='CASCADE_RUN_STATUS_IDLE', generators=None, on_switch='run', model_after=None):
        self.status = status
        self.generators = generators if generators is not None else [self.gen('MODEL_PLACEHOLDER_M320', knowledge=True)]
        self.on_switch, self.model_after = on_switch, model_after
        self.calls = []

    @staticmethod
    def gen(model, knowledge=False):
        return {'plannerConfig': {'planModel': model, 'modelName': 'x', 'knowledgeConfig': {'enabled': knowledge},
                                  'maxOutputTokens': 100000}}

    def summary(self):
        return {'status': self.status, 'stepCount': str(len(self.generators) * 2), 'workspaces': []}

    def rpc(self, url, csrf, method, payload, timeout=5):
        self.calls.append((method, payload))
        if method == 'GetCascadeModelConfigData':
            return {'clientModelConfigs': CATALOG}
        if method == 'GetCascadeTrajectoryGeneratorMetadata':
            rows = self.generators[payload.get('generatorMetadataOffset', 0):]
            return {'generatorMetadata': rows} if rows else {}
        if method == 'GetAllCascadeTrajectories':
            return {'trajectorySummaries': {SID: self.summary()}}
        if method == 'SendUserCascadeMessage':
            if self.on_switch == 'refuse':
                raise agy_live.DeliveryError('Antigravity SendUserCascadeMessage HTTP 500: busy')
            if self.on_switch == 'drop':
                raise agy_live.DeliveryError('Antigravity SendUserCascadeMessage connection failed: timed out')
            if self.on_switch == 'run':
                model = self.model_after or payload['cascadeConfig']['plannerConfig']['requestedModel']['model']
                self.generators.append({'plannerConfig': {**payload['cascadeConfig']['plannerConfig'], 'planModel': model}})
            return {}
        raise AssertionError(method)

    def patches(self):
        server = {'url': 'http://127.0.0.1:1', 'csrf': 'x', 'pid': 1, 'summaries': {SID: self.summary()}}
        row = {'status': agy_live.normalize_status(self.status), 'cwd': '/tmp'}
        return (patch.object(agy_live, 'rpc_call', self.rpc),
                patch.object(agy_live, 'resolve_agy', lambda sid, timeout=5: (server, row)))


class AgyModelTest(unittest.TestCase):
    def switch(self, fake, model='gemini-3.8-flash-medium', timeout=2):
        a, b = fake.patches()
        with a, b:
            return agy_live.set_model(SID, model, timeout=timeout, poll=0.01)

    def sent(self, fake):
        return [p for m, p in fake.calls if m == 'SendUserCascadeMessage']

    def test_read_names_the_last_generator_model(self):
        fake = FakeAgy(generators=[FakeAgy.gen('MODEL_PLACEHOLDER_M319')] * 5 + [FakeAgy.gen('MODEL_PLACEHOLDER_M320')])
        a, b = fake.patches()
        with a, b:
            result = agy_live.read_model(SID)
        self.assertEqual((result['model'], result['label'], result['generators']),
                         ('gemini-3.8-flash-low', 'Gemini 3.8 Flash (Low)', 6))
        offsets = [p['generatorMetadataOffset'] for m, p in fake.calls if m == 'GetCascadeTrajectoryGeneratorMetadata']
        self.assertEqual(offsets[0], 11)  # stepCount - 1: only the tail is fetched first

    def test_read_without_a_turn_is_unknown(self):
        fake = FakeAgy(generators=[])
        a, b = fake.patches()
        with a, b:
            self.assertEqual(agy_live.read_model(SID)['model'], 'unknown')

    def test_switch_preserves_the_planner_and_changes_only_the_model(self):
        fake = FakeAgy()
        result = self.switch(fake)
        self.assertEqual((result['model'], result['previous_model'], result['changed']),
                         ('gemini-3.8-flash-medium', 'gemini-3.8-flash-low', True))
        (frame,) = self.sent(fake)
        planner = frame['cascadeConfig']['plannerConfig']
        self.assertEqual(planner['requestedModel'], {'model': 'MODEL_PLACEHOLDER_M319'})
        self.assertEqual((planner['knowledgeConfig'], planner['maxOutputTokens']), ({'enabled': True}, 100000))
        self.assertNotIn('planModel', planner)
        self.assertNotIn('modelName', planner)
        self.assertEqual(frame['deliveryStrategy'], agy_live.NEXT_INVOCATION)
        self.assertNotIn('items', frame)

    def test_switch_to_the_current_model_sends_nothing(self):
        fake = FakeAgy()
        result = self.switch(fake, 'Gemini 3.8 Flash (Low)')
        self.assertEqual((result['changed'], self.sent(fake)), (False, []))

    def test_running_conversation_is_refused_before_any_send(self):
        fake = FakeAgy(status='CASCADE_RUN_STATUS_RUNNING')
        with self.assertRaises(agy_live.ModelSwitchError) as caught:
            self.switch(fake)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(self.sent(fake), [])

    def test_http_refusal_is_definite_and_transport_loss_is_uncertain(self):
        for mode, uncertain in (('refuse', False), ('drop', True)):
            fake = FakeAgy(on_switch=mode)
            with self.assertRaises(agy_live.ModelSwitchError) as caught:
                self.switch(fake)
            self.assertEqual(caught.exception.uncertain, uncertain)
            self.assertEqual(len(self.sent(fake)), 1)

    def test_no_invocation_or_wrong_readback_is_uncertain_and_never_resent(self):
        for fake in (FakeAgy(on_switch='silent'), FakeAgy(model_after='MODEL_PLACEHOLDER_M320')):
            with self.assertRaises(agy_live.ModelSwitchError) as caught:
                self.switch(fake, timeout=0.05)
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(len(self.sent(fake)), 1)

    def test_unknown_model_is_refused_before_any_send(self):
        fake = FakeAgy()
        with self.assertRaisesRegex(agy_live.DeliveryError, 'Unknown Antigravity model'):
            self.switch(fake, 'no-such-model')
        self.assertEqual(self.sent(fake), [])

    def test_conversation_without_a_planner_is_refused(self):
        fake = FakeAgy(generators=[{'chatModel': {'model': 'MODEL_PLACEHOLDER_M320'}}])
        with self.assertRaisesRegex(agy_live.ModelSwitchError, 'no planner configuration'):
            self.switch(fake)
        self.assertEqual(self.sent(fake), [])


class ClaudeReadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sid = str(uuid.uuid4())
        (self.root / '-work').mkdir()
        self.path = self.root / '-work' / f'{self.sid}.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, *entries, pad=0):
        lines = [json.dumps(e) for e in entries]
        lines.insert(len(lines) - 1 if pad else len(lines), json.dumps({'type': 'user', 'pad': 'x' * pad}))
        self.path.write_text('\n'.join(lines) + '\n')

    def test_newest_real_assistant_model_wins(self):
        self.write({'type': 'assistant', 'message': {'model': 'claude-opus-5-5'}},
                   {'type': 'assistant', 'message': {'model': 'claude-sonnet-5-5'}},
                   {'type': 'assistant', 'message': {'model': '<synthetic>'}})
        self.assertEqual(models.transcript_model(self.sid, self.root), ('claude-sonnet-5-5', None))

    def test_reads_past_a_long_tail_in_growing_chunks(self):
        self.write({'type': 'assistant', 'message': {'model': 'claude-opus-5-5'}},
                   {'type': 'user', 'message': {'content': 'last'}}, pad=50000)
        self.assertEqual(models.transcript_model(self.sid, self.root, chunk=1024)[0], 'claude-opus-5-5')

    def test_missing_or_empty_transcript_is_unknown_with_a_reason(self):
        self.assertIsNone(models.transcript_model(self.sid, self.root)[0])
        self.write({'type': 'user'})
        self.assertEqual(models.transcript_model(self.sid, self.root), (None, 'no assistant message yet'))

    def test_claude_switch_names_the_host_gap(self):
        for host, text in (('claude-vscode', 'model picker'), ('claude-desktop', 'no bridge relay'),
                           ('claude-other', 'no bridge-reachable')):
            with patch.object(models, 'claude_host', return_value=host):
                with self.assertRaisesRegex(models.Unsupported, text):
                    models.claude_switch({'session': self.sid}, 'claude-sonnet-5-5', 1)


POST_SID = str(uuid.uuid4())
TARGET_SID = str(uuid.uuid4())


class FakeRelay:
    def __init__(self, routes, reply=None, timed_out=False):
        self.route_rows, self.reply, self.timed_out, self.asked = routes, reply, timed_out, []

    def routes(self, project):
        return self.route_rows

    def ask(self, project, recipient, cwd, body, key, timeout):
        self.asked.append({'project': project, 'recipient': recipient, 'cwd': cwd, 'body': body, 'key': key,
                           'timeout': timeout})
        replies = [] if self.timed_out else [{'body': self.reply}]
        return {'id': 'm1'}, {'id': 'm1', 'timed_out': self.timed_out, 'replies': replies}


class ClaudeDesktopSwitchTest(unittest.TestCase):
    TARGET = {'project': 'demo', 'name': 'worker', 'transport': 'claude-peer', 'session': TARGET_SID, 'cwd': '/w'}
    POST = {'project': 'demo', 'name': 'hq', 'transport': 'claude-peer', 'session': POST_SID, 'cwd': '/hq'}

    def policy(self, post='hq'):
        return {'path': '/state/policy.json', 'exists': True, 'defaults': {}, 'deny': {},
                'command_post': {'claude': post} if post else {}}

    def switch(self, relay, policy=None, hosts=None):
        hosts = hosts or {TARGET_SID: 'claude-desktop', POST_SID: 'claude-desktop'}
        with patch.object(models, 'claude_host', side_effect=lambda sid, *a: hosts[sid]), \
                patch.object(models, 'desktop_session', return_value='local_abc'):
            return models.switch(self.TARGET, 'claude-sonnet-5-5', policy or self.policy(), 30, relay)

    def test_relays_one_request_line_and_returns_the_reading(self):
        reply = 'done\nbridge-model-reply: {"model": "claude-sonnet-5-5", "changed": true, "applies": "next turn"}'
        relay = FakeRelay([self.TARGET, self.POST], reply)
        out = self.switch(relay)
        self.assertEqual({k: out[k] for k in ('provider', 'model', 'changed', 'applies', 'via', 'session')},
                         {'provider': 'claude', 'model': 'claude-sonnet-5-5', 'changed': True, 'applies': 'next turn',
                          'via': 'hq', 'session': TARGET_SID})
        (asked,) = relay.asked
        self.assertEqual((asked['recipient'], asked['cwd'], asked['timeout']), ('hq', '/hq', 30))
        self.assertEqual(asked['body'].count('\n'), 0)
        self.assertTrue(asked['body'].startswith(models.REQUEST_PREFIX))
        request = json.loads(asked['body'][len(models.REQUEST_PREFIX):])
        self.assertEqual({k: request[k] for k in ('op', 'session_id', 'desktop_session', 'model', 'target', 'reply')},
                         {'op': 'set_session_model', 'session_id': TARGET_SID, 'desktop_session': 'local_abc',
                          'model': 'claude-sonnet-5-5', 'target': 'worker', 'reply': models.REPLY_PREFIX})
        self.assertEqual(asked['key'], 'model-request:' + request['id'])

    def test_no_command_post_names_the_policy_key_and_sends_nothing(self):
        relay = FakeRelay([self.TARGET, self.POST])
        with self.assertRaisesRegex(models.Unsupported, r'"command_post": \{"claude": "<route name>"\} in /state/policy.json'):
            self.switch(relay, self.policy(None))
        self.assertEqual(relay.asked, [])
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(models.load_policy(state)['command_post'], {})

    def test_command_post_must_be_a_live_desktop_claude_peer_other_than_the_target(self):
        cases = (
            ([self.TARGET], None, 'not registered'),
            ([self.TARGET, {**self.POST, 'transport': 'codex'}], None, 'must be a claude-peer route'),
            ([self.TARGET, {**self.POST, 'session': TARGET_SID}], None, 'refuses set_session_model on the calling'),
            ([self.TARGET, self.POST], {TARGET_SID: 'claude-desktop', POST_SID: 'claude-vscode'}, 'only a desktop-hosted'),
        )
        for routes, hosts, text in cases:
            relay = FakeRelay(routes)
            with self.subTest(text), self.assertRaisesRegex(models.Unsupported, text):
                self.switch(relay, hosts=hosts)
            self.assertEqual(relay.asked, [])
        relay = FakeRelay([self.TARGET, self.POST])
        def host(sid, *a):
            if sid == POST_SID:
                raise sessions.DiscoveryError('gone')
            return 'claude-desktop'
        with patch.object(models, 'claude_host', side_effect=host), \
                patch.object(models, 'desktop_session', return_value=None), \
                self.assertRaisesRegex(models.Unsupported, 'is not running'):
            models.switch(self.TARGET, 'm', self.policy(), 30, relay)

    def test_timeout_and_unreadable_replies_are_uncertain_and_an_error_reply_is_reported(self):
        for relay, uncertain, text in (
                (FakeRelay([self.TARGET, self.POST], timed_out=True), True, 'did not answer'),
                (FakeRelay([self.TARGET, self.POST], 'ok, switched'), True, 'without a bridge-model-reply'),
                (FakeRelay([self.TARGET, self.POST], 'bridge-model-reply: {"model": "x"}'), True, 'no model/changed'),
                (FakeRelay([self.TARGET, self.POST], 'bridge-model-reply: {"error": "user declined"}'), False, 'user declined')):
            with self.subTest(text), self.assertRaisesRegex(models.SwitchError, text) as caught:
                self.switch(relay)
            self.assertEqual(caught.exception.uncertain, uncertain)

    def test_denied_model_never_reaches_the_command_post(self):
        relay = FakeRelay([self.TARGET, self.POST], 'bridge-model-reply: {"model": "m", "changed": true}')
        policy = {**self.policy(), 'deny': {'claude': ['claude-sonnet-5-5']}}
        with self.assertRaisesRegex(ValueError, 'denylist'):
            self.switch(relay, policy)
        self.assertEqual(relay.asked, [])

    def test_desktop_session_is_the_registry_host_id_only_when_it_is_a_desktop_one(self):
        for record, expected in (({'hostSessionId': 'local_x'}, 'local_x'), ({'hostSessionId': 'cli'}, None), ({}, None)):
            with patch.object(models, 'claude_record', return_value=record):
                self.assertEqual(models.desktop_session(TARGET_SID), expected)
        with patch.object(models, 'claude_record', side_effect=sessions.DiscoveryError('gone')):
            self.assertIsNone(models.desktop_session(TARGET_SID))

    def test_malformed_command_post_policy_is_an_error(self):
        with tempfile.TemporaryDirectory() as state:
            for text in ('{"command_post": "hq"}', '{"command_post": {"claude": ""}}'):
                Path(state, 'policy.json').write_text(text)
                with self.assertRaisesRegex(ValueError, 'command_post'):
                    models.load_policy(state)


class ClaudeDesktopRelayCommandTest(unittest.TestCase):
    """`model --set` end to end through a real broker; only the push into the command post is faked."""

    def test_request_reaches_the_command_post_and_its_reply_answers_the_command(self):
        with RunningBroker() as broker:
            for name, sid in (('worker', TARGET_SID), ('hq', POST_SID)):
                broker.call('register', project='demo', name=name, cwd=broker.temp.name, transport='claude-peer',
                            session=sid)
            Path(broker.state_dir).mkdir(parents=True, exist_ok=True)
            Path(broker.state_dir, 'policy.json').write_text('{"command_post": {"claude": "hq"}}')
            pushed = []

            def command_post(path, m):
                pushed.append(m)
                line = models.REPLY_PREFIX + ' ' + json.dumps({'model': 'claude-sonnet-5-5', 'changed': True,
                                                               'applies': 'next turn'})
                bridge.broker.request(path, {'op': 'reply', 'id': m['id'], 'body': line, 'key': 'reply-' + m['id']})
                return {**m, 'state': 'submitted'}
            with patch.object(bridge, 'push', side_effect=command_post), \
                    patch.object(models, 'claude_host', return_value='claude-desktop'), \
                    patch.object(models, 'desktop_session', return_value=None):
                code, out = run(['--socket', broker.path, 'model', 'worker', '--project', 'demo',
                                 '--set', 'claude-sonnet-5-5', '--timeout', '10', '--state-dir', broker.state_dir])
            self.assertEqual(code, 0, out)
            self.assertEqual((out['model'], out['changed'], out['applies'], out['via']),
                             ('claude-sonnet-5-5', True, 'next turn', 'hq'))
            (m,) = pushed
            self.assertEqual((m['recipient'], m['sender'], m['route']['session']), ('hq', models.RELAY_NAME, POST_SID))
            self.assertTrue(m['body'].startswith(models.REQUEST_PREFIX))
            self.assertNotIn('desktop_session', json.loads(m['body'][len(models.REQUEST_PREFIX):]))
            relay = [r for r in broker.call('routes', project='demo') if r['name'] == models.RELAY_NAME]
            self.assertEqual(relay[0]['transport'], 'realtime')

    def test_a_non_realtime_route_holding_the_relay_name_is_refused(self):
        with RunningBroker() as broker:
            for name, sid in (('worker', TARGET_SID), ('hq', POST_SID), (models.RELAY_NAME, str(uuid.uuid4()))):
                broker.call('register', project='demo', name=name, cwd=broker.temp.name, transport='claude-peer',
                            session=sid)
            Path(broker.state_dir).mkdir(parents=True, exist_ok=True)
            Path(broker.state_dir, 'policy.json').write_text('{"command_post": {"claude": "hq"}}')
            with patch.object(bridge, 'push') as pushed, \
                    patch.object(models, 'claude_host', return_value='claude-desktop'):
                code, out = run(['--socket', broker.path, 'model', 'worker', '--project', 'demo',
                                 '--set', 'claude-sonnet-5-5', '--state-dir', broker.state_dir])
            self.assertEqual(code, 1)
            self.assertIn('realtime sender', out['error'])
            pushed.assert_not_called()


if __name__ == '__main__':
    unittest.main()
