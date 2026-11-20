"""Correlate new-thread wake attestation to the exact Desktop queue turn."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import codex_wake_open


THREAD = '00000000-0000-4000-8000-000000000101'
TURN = '00000000-0000-4000-8000-000000000102'
UNRELATED_TURN = '00000000-0000-4000-8000-000000000103'
CLIENT_ID = '00000000-0000-4000-8000-000000000105'
CWD = Path('/private/tmp/fake-codex-wake-workspace')
MODEL = 'gpt-6.2-sol'
PROMPT = 'Requested Agent Bridge startup check. Reply exactly AB_WAKE_TEST_OK and use no tools.'
REPLY = codex_wake_open.MARKER


def event(event_type, turn=TURN, **fields):
    return {'type': 'event_msg', 'payload': {'type': event_type, 'turn_id': turn, **fields}}


def workspace_permissions():
    # Native Desktop turn_context, with only the workspace path anonymized.
    return {
        'active_permission_profile': {'id': ':workspace'},
        'approval_policy': 'on-request',
        'sandbox_policy': {
            'type': 'workspace-write', 'network_access': False,
            'exclude_tmpdir_env_var': False, 'exclude_slash_tmp': False,
        },
        'permission_profile': {
            'type': 'managed', 'network': 'restricted',
            'file_system': {'type': 'restricted', 'entries': [
                {'path': {'type': 'special', 'value': {'kind': 'root'}}, 'access': 'read'},
                {'path': {'type': 'path', 'path': str(CWD)}, 'access': 'write'},
                {'path': {'type': 'special', 'value': {'kind': 'slash_tmp'}}, 'access': 'write'},
                {'path': {'type': 'special', 'value': {'kind': 'tmpdir'}}, 'access': 'write'},
                *({'path': {'type': 'path', 'path': str(CWD / child)},
                   'access': 'read', 'missing_path_behavior': 'skip'}
                  for child in ('.git', '.agents', '.codex', '.aws')),
            ]},
        },
    }


def startup_records(turn=TURN, trigger='queue', model=MODEL, prompt=PROMPT, reply=REPLY):
    return [
        {'type': 'session_meta', 'payload': {'id': THREAD, 'session_id': THREAD, 'cwd': str(CWD)}},
        event('task_started', turn, root_turn_id=turn, turn_attribution={
            'turn_id': turn, 'root_turn_id': turn, 'parent_turn_id': None, 'turn_trigger': trigger,
        }),
        {'type': 'turn_context', 'payload': {
            'turn_id': turn, 'root_turn_id': turn, 'cwd': str(CWD), 'model': model,
            'active_permission_profile': {'id': ':read-only'},
            'permission_profile': codex_wake_open.READ_ONLY_PROFILE,
            'approval_policy': 'on-request', 'sandbox_policy': {'type': 'read-only'},
        }},
        event('item_completed', turn, thread_id=THREAD, item={
            'type': 'UserMessage', 'client_id': CLIENT_ID,
            'content': [{'type': 'text', 'text': prompt, 'text_elements': []}],
        }),
        event('item_completed', turn, thread_id=THREAD, item={
            'type': 'AgentMessage', 'phase': 'final_answer',
            'content': [{'type': 'Text', 'text': reply}],
        }),
        event('task_complete', turn, root_turn_id=turn, last_agent_message=reply),
    ]


class DesktopWakeAttestationTests(unittest.TestCase):
    def setUp(self):
        self.attestation = codex_wake_open._Attestation(THREAD, CWD, MODEL, PROMPT)

    def feed(self, records):
        return [result for record in records if (result := self.attestation.feed(record)) is not None]

    def test_exact_queued_turn_reports_read_only_permission_without_carrying_composer_settings(self):
        startup = startup_records()
        self.assertEqual(self.feed(startup[:-1]), [])
        result, = self.feed(startup[-1:])
        self.assertEqual(result['turn_id'], TURN)
        self.assertEqual(result['model'], MODEL)
        self.assertEqual(result['permission_settings']['approvalPolicy'], 'on-request')
        self.assertEqual(result['permission_settings']['sandboxPolicy']['type'], 'readOnly')
        self.assertEqual(result['permission_settings']['activePermissionProfile'], {'id': ':read-only'})

    def test_exact_queued_turn_reports_scoped_workspace_write_permissions(self):
        startup = startup_records()
        startup[2]['payload'].update(workspace_permissions())
        result, = self.feed(startup)
        self.assertEqual(result, {
            'turn_id': TURN, 'model': MODEL, 'permission_settings': {
                'activePermissionProfile': {'id': ':workspace'},
                'approvalPolicy': 'on-request',
                'sandboxPolicy': {'type': 'workspaceWrite', 'networkAccess': False},
            },
        })

    def test_workspace_turn_refuses_broader_root_or_network_and_unknown_permissions(self):
        def root_write(context):
            context['permission_profile']['file_system']['entries'][0]['access'] = 'write'

        def outside_workspace(context):
            context['permission_profile']['file_system']['entries'][1]['path']['path'] = str(CWD.parent)

        def sandbox_network(context):
            context['sandbox_policy']['network_access'] = True

        def profile_network(context):
            context['permission_profile']['network'] = 'enabled'

        def unknown_profile(context):
            context['active_permission_profile']['id'] = ':custom'

        for mutate in (root_write, outside_workspace, sandbox_network, profile_network, unknown_profile):
            with self.subTest(mutation=mutate.__name__):
                startup = startup_records()
                context = startup[2]['payload']
                context.update(workspace_permissions())
                mutate(context)
                self.attestation = codex_wake_open._Attestation(THREAD, CWD, MODEL, PROMPT)
                with self.assertRaises(codex_wake_open.WakeError):
                    self.feed(startup)

    def test_composer_turn_with_matching_marker_cannot_attest(self):
        with self.assertRaises(codex_wake_open.WakeError):
            self.feed(startup_records(turn=UNRELATED_TURN, trigger='composer'))

    def test_completion_on_different_turn_cannot_attest(self):
        startup = startup_records()
        startup[-1]['payload']['turn_id'] = UNRELATED_TURN
        with self.assertRaises(codex_wake_open.WakeError):
            self.feed(startup)

    def test_different_queued_payload_cannot_attest(self):
        startup = startup_records(prompt=PROMPT + ' Extra text.')
        with self.assertRaises(codex_wake_open.WakeError):
            self.feed(startup)

    def test_exact_challenge_with_wrong_model_is_refused(self):
        with self.assertRaises(codex_wake_open.WakeError):
            self.feed(startup_records(model='other-model'))

    def test_final_marker_with_extra_text_cannot_attest(self):
        startup = startup_records()
        startup[-2]['payload']['item']['content'][0]['text'] = REPLY + ' Extra text.'
        with self.assertRaises(codex_wake_open.WakeError):
            self.feed(startup)


    def test_failed_attestation_never_requeues_or_reopens_desktop(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / 'rollout.jsonl'
            log.write_text(json.dumps(startup_records()[0]) + '\n')
            desktop = codex_wake_open.Desktop(Path(root) / 'state.sqlite',
                                              Path(root) / 'queue.sqlite', ('owner', 'parent', 'app'))
            with patch.object(codex_wake_open, '_saved_rollout', return_value=(CWD, log)), \
                    patch.object(codex_wake_open, '_owner', return_value=desktop.owner), \
                    patch.object(codex_wake_open.codex_queue, 'enqueue') as enqueue, \
                    patch.object(codex_wake_open.subprocess, 'run') as opener, \
                    patch.object(codex_wake_open, '_tail',
                                 side_effect=codex_wake_open.WakeError('ambiguous settle')):
                with self.assertRaisesRegex(codex_wake_open.WakeError, 'ambiguous settle'):
                    codex_wake_open.adopt(desktop, {'session': THREAD}, MODEL, timeout=3)
            enqueue.assert_called_once()
            opener.assert_called_once()
            self.assertEqual(enqueue.call_args.args[0], THREAD)
            self.assertEqual(opener.call_args.args[0][-1], 'codex://threads/wake/' + THREAD)


class DesktopWakeRolloutTests(unittest.TestCase):
    """adopt() over a real rollout file: only the queue write, the app link and the store row are faked."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.cwd = root / 'workspace'
        self.log = root / 'home' / 'sessions' / f'rollout-2026-01-01T00-00-00-{THREAD}.jsonl'
        self.cwd.mkdir()
        self.log.parent.mkdir(parents=True)
        self.desktop = codex_wake_open.Desktop(root / 'home' / 'state.sqlite', root / 'home' / 'queue.sqlite',
                                               ('owner', 'parent', 'app'))
        self.route = {'transport': 'codex', 'session': THREAD, 'cwd': str(self.cwd)}
        self.append(self.records(PROMPT)[:1])
        self.queued = []
        for target, name, value in (
                (codex_wake_open.codex_preview, '_saved_thread_row',
                 lambda route, include_rollout=False, path=None: (str(self.cwd), 0, str(self.log))),
                (codex_wake_open, '_owner', lambda queue, remaining: self.desktop.owner),
                (codex_wake_open.codex_queue, 'enqueue', self.enqueue)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        opener = patch.object(codex_wake_open.subprocess, 'run')
        self.opener = opener.start()
        self.addCleanup(opener.stop)

    def records(self, prompt, **changes):
        text = json.dumps(startup_records(prompt=prompt, **changes)).replace(str(CWD), str(self.cwd))
        return json.loads(text)

    def append(self, records):
        with self.log.open('a') as output:
            output.write(''.join(json.dumps(record) + '\n' for record in records))

    def enqueue(self, thread, proof, timeout):
        self.queued.append((thread, proof))
        self.desktop_turn(proof)

    def desktop_turn(self, proof):
        self.append(self.records(proof)[1:])

    def test_the_queued_turn_written_after_the_call_is_the_evidence(self):
        result = codex_wake_open.adopt(self.desktop, self.route, MODEL, timeout=5)
        (thread, proof), = self.queued
        self.assertEqual(thread, THREAD)
        self.assertTrue(proof.startswith(codex_wake_open.PROMPT))
        self.assertEqual(result, {'model': MODEL, 'turn_id': TURN, 'permission_settings': {
            'activePermissionProfile': {'id': ':read-only'}, 'approvalPolicy': 'on-request',
            'sandboxPolicy': {'type': 'readOnly'}}})
        self.opener.assert_called_once()

    def test_a_turn_already_in_the_file_or_carrying_another_nonce_is_not_evidence(self):
        earlier = codex_wake_open.PROMPT + '00000000-0000-4000-8000-000000000999'
        self.append(self.records(earlier)[1:])  # A complete matching-looking turn before the call.
        self.desktop_turn = lambda proof: self.append(self.records(earlier, turn=UNRELATED_TURN)[1:])
        with self.assertRaisesRegex(codex_wake_open.WakeError, codex_wake_open.EVIDENCE_ERROR):
            codex_wake_open.adopt(self.desktop, self.route, MODEL, timeout=5)
        self.assertEqual(len(self.queued), 1)

    def test_no_turn_ends_at_the_deadline_with_one_queue_write(self):
        self.desktop_turn = lambda proof: None
        with self.assertRaisesRegex(codex_wake_open.WakeError, 'deadline expired'):
            codex_wake_open.adopt(self.desktop, self.route, MODEL, timeout=.4)
        self.assertEqual(len(self.queued), 1)
        self.opener.assert_called_once()

    def test_a_rollout_outside_the_sessions_store_or_for_another_workspace_is_refused_before_queueing(self):
        for route in (dict(self.route, cwd=str(self.cwd.parent)), dict(self.route, transport='agy')):
            with self.subTest(route=route), self.assertRaisesRegex(codex_wake_open.WakeError,
                                                                   codex_wake_open.TARGET_ERROR):
                codex_wake_open.adopt(self.desktop, route, MODEL, timeout=5)
        moved = self.log.parent.parent / self.log.name
        self.log.rename(moved)
        self.log = moved
        with self.assertRaisesRegex(codex_wake_open.WakeError, codex_wake_open.TARGET_ERROR):
            codex_wake_open.adopt(self.desktop, self.route, MODEL, timeout=5)
        self.assertEqual(self.queued, [])
        self.opener.assert_not_called()
