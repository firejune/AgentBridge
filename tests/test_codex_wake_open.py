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
