"""The HQ shared contract. No provider, clock, filesystem or socket dependency."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import provider_failures as policy


class PolicyTests(unittest.TestCase):
    def test_defaults_are_immutable_and_cap_stops(self):
        value = policy.retry_policy()
        self.assertEqual(value.backoff, (15, 30, 60))
        self.assertEqual([policy.next_delay(value, n) for n in range(5)], [15, 30, 60, None, None])
        with self.assertRaises(FrozenInstanceError):
            value.cap = 9

    def test_short_schedule_reuses_last_delay(self):
        value = policy.retry_policy((7,), 4)
        self.assertEqual([policy.next_delay(value, n) for n in range(5)], [7, 7, 7, 7, None])

    def test_invalid_policy_and_attempt_counts(self):
        for cap in (0, -1, True, 1.5, '3', None):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                policy.retry_policy(cap=cap)
        for backoff in ([], [15], (), (0,), (-1,), (True,), (1.5,), ('15',), None):
            with self.subTest(backoff=backoff), self.assertRaises(ValueError):
                policy.retry_policy(backoff=backoff)
        for count in (-1, True, 1.5, '1', None):
            with self.subTest(count=count), self.assertRaises(ValueError):
                policy.next_delay(policy.retry_policy(), count)

    def body(self, event=policy.RETRY_EVENT, **fields):
        values = dict(provider='codex', kind='responseStreamDisconnected', failed_at=None,
                      correlation='fixture-turn', attempt=1, cap=3)
        values.update(fields)
        return policy.retry_event_body(event, **values)

    def test_all_event_names_and_both_provider_correlations(self):
        for event in (policy.DETECTED_EVENT, policy.UNSAFE, policy.UNCERTAIN):
            result = self.body(event, attempt=None, cap=None)
            self.assertEqual(result['event'], event)
            self.assertEqual(list(result), sorted(result))
            self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(self.body(policy.EXHAUSTED, attempt=3)['attempt'], 3)
        self.assertEqual(self.body(provider='agy', correlation='agy:12', kind='provider_header_timeout',
                                   failed_at='2026-11-09T00:00:00Z')['provider'], 'agy')

    def test_invalid_event_fields(self):
        cases = [dict(event='unknown'), dict(provider='other'), dict(kind=None), dict(kind='raw provider error!'),
                 dict(correlation=None), dict(correlation=''), dict(correlation='raw text'),
                 dict(failed_at=123), dict(failed_at='bad'), dict(failed_at='2026-11-09T00:00:00'),
                 dict(failed_at='2026-11-09T00:00:00+09:00'), dict(attempt=None), dict(cap=None),
                 dict(attempt=True), dict(cap=True), dict(attempt=0), dict(attempt=4), dict(cap=0),
                 dict(event=policy.EXHAUSTED, attempt=2), dict(event=policy.UNSAFE),
                 dict(event=policy.UNCERTAIN), dict(event=policy.DETECTED_EVENT)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.body(**case)
