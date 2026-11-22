"""Opt-in live canary (#200): does the installed Claude Code still lay out its peer inbox the way claude-peer expects?

Skipped unless AGENT_BRIDGE_LIVE_CANARY=1. Read-only: it lists the live-session registry and reads key files
into memory. It never connects to a messaging socket, sends nothing, and never prints a token.
Run it from inside a Claude Code session so at least one live record exists:

    AGENT_BRIDGE_LIVE_CANARY=1 PYTHONPATH=skill/agent-bridge/scripts:tests \
        python3 -m unittest test_live_vendor_canary_claude
"""
import json
import os
from pathlib import Path
import re
import stat
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delivery
import sessions

LIVE = os.environ.get('AGENT_BRIDGE_LIVE_CANARY') == '1'
RECORD_FIELDS = ('pid', 'sessionId', 'peerProtocol', 'messagingSocketPath')


def live_records(root):
    for path in sorted(root.iterdir()):
        if not re.fullmatch(r'\d+\.json', path.name):
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue  # Rewritten while a session updates its status.
        if isinstance(record, dict) and isinstance(record.get('pid'), int) and sessions.pid_alive(record['pid']):
            yield path, record


@unittest.skipUnless(LIVE, 'set AGENT_BRIDGE_LIVE_CANARY=1 to read the installed Claude Code peer layout')
class ClaudePeerLayoutCanary(unittest.TestCase):
    def setUp(self):
        self.root = sessions.claude_sessions_dir()
        self.assertTrue(self.root.is_dir(), f'Claude live-session registry is missing: {self.root}')
        self.records = list(live_records(self.root))
        with_inbox = [(p, r) for p, r in self.records if 'messagingSocketPath' in r]
        if not with_inbox:
            self.skipTest('no live Claude session with its inbox on; run this from a Claude Code session')
        self.with_inbox = with_inbox

    def test_live_records_carry_the_fields_and_protocol_the_resolver_reads(self):
        for path, record in self.with_inbox:
            with self.subTest(record=path.name):
                missing = [field for field in RECORD_FIELDS if field not in record]
                self.assertEqual(missing, [], 'registry record fields changed')
                self.assertEqual(path.name, f"{record['pid']}.json")
                self.assertEqual(record['peerProtocol'], sessions.PEER_PROTOCOL, 'peerProtocol changed')
                self.assertTrue(record['messagingSocketPath'].startswith('/'))
                mode = os.stat(record['messagingSocketPath']).st_mode
                self.assertTrue(stat.S_ISSOCK(mode), 'messagingSocketPath is not a Unix socket')

    def test_key_file_sits_where_and_holds_what_the_transport_reads(self):
        for path, record in self.with_inbox:
            with self.subTest(record=path.name):
                target = sessions.resolve_claude_peer(record['sessionId'], self.root)
                self.assertEqual((target['pid'], target['socket']), (record['pid'], record['messagingSocketPath']))
                # peer_token checks name (<pid>.<sha256(abs socket)>.key), JSON shape and 32 hex digits.
                token = delivery.peer_token(target)
                self.assertIsInstance(token, str)
                del token  # Never printed, logged or kept.

    def test_this_session_is_reachable_when_its_id_is_known(self):
        sid = os.environ.get('CLAUDE_CODE_SESSION_ID')
        if not sid:
            self.skipTest('CLAUDE_CODE_SESSION_ID is not set')
        target = sessions.resolve_claude_peer(sid, self.root)
        self.assertEqual(target['peer_protocol'], sessions.PEER_PROTOCOL)
        delivery.peer_token(target)


if __name__ == '__main__':
    unittest.main()
