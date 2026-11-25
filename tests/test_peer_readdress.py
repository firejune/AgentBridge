"""Issue #257: pending mail to an unreachable Claude session follows its route name to a new session."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
from support import RunningBroker

UNREACHABLE = {'state': 'unsupported', 'code': 'no_desktop_session', 'reason': 'gone'}


class ReaddressTests(unittest.TestCase):
    def setUp(self):
        self.old, self.new = str(uuid.uuid4()), str(uuid.uuid4())
        self.keys = 0

    def start(self, server):
        self.cwd = server.temp.name
        server.call('register', project='demo', name='codex-worker', cwd=self.cwd)
        server.call('register', project='demo', name='claude-tower', cwd=self.cwd,
                    transport='claude-peer', session=self.old)

    def send(self, server, channel=None):
        self.keys += 1
        fields = {'channel': channel} if channel else {'recipient': 'claude-tower'}
        return server.call('send', project='demo', sender='codex-worker', body='DONE',
                           key=f'k{self.keys}', **fields)['id']

    def fail_push(self, server, mid, wake=UNREACHABLE):
        server.call('claim', id=mid)
        return server.call('finish', id=mid, state='pending', event='push_failed',
                           note='push_failed: session not running', wake=wake)

    def reregister(self, server, cwd=None):
        server.call('unregister', project='demo', name='claude-tower')
        return server.call('register', project='demo', name='claude-tower', cwd=cwd or self.cwd,
                           transport='claude-peer', session=self.new)

    def inbox(self, server):
        return {row['id']: row for row in server.call('inbox', project='demo', name='claude-tower')}

    def test_pending_reply_to_an_unreachable_session_moves_and_stays_moved_after_restart(self):
        with RunningBroker() as server:
            self.start(server)
            mid = self.send(server)
            self.fail_push(server, mid)
            self.assertTrue(server.call('show', id=mid)['wakes'])
            self.assertFalse(self.inbox(server)[mid]['route_dropped'])
            server.call('unregister', project='demo', name='claude-tower')
            self.assertTrue(self.inbox(server)[mid]['route_dropped'])
            result = server.call('register', project='demo', name='claude-tower', cwd=self.cwd,
                                 transport='claude-peer', session=self.new)
            self.assertEqual((result['readdressed'], result['readdressed_ids']), (1, [mid]))
            self.assertNotIn('readdressed', server.call('routes', project='demo')[-1])
            moved = server.call('show', id=mid)
            self.assertEqual((moved['state'], moved['route']['session']), ('pending', self.new))
            self.assertNotIn('wakes', moved)
            self.assertIn('readdressed from Claude session', moved['note'])
            self.assertFalse(self.inbox(server)[mid]['route_dropped'])
            # Retrying the same registration moves nothing more.
            again = server.call('register', project='demo', name='claude-tower', cwd=self.cwd,
                                transport='claude-peer', session=self.new)
            self.assertEqual(again['readdressed'], 0)
            server.restart()
            kept = server.call('show', id=mid)
            self.assertEqual((kept['state'], kept['route']['session']), ('pending', self.new))

    def test_nothing_moves_without_wake_evidence_when_submitted_on_a_channel_or_in_another_directory(self):
        with RunningBroker() as server:
            self.start(server)
            silent = self.send(server)  # never pushed: no wake record
            reached = self.send(server)
            server.call('claim', id=reached)
            server.call('finish', id=reached, state='submitted', event='pushed', note='pushed', wake=UNREACHABLE)
            server.call('channel-bind', project='demo', name='handoff', owner='claude-tower', description='handoff')
            on_channel = self.send(server, channel='handoff')
            self.fail_push(server, on_channel)
            server.call('channel-close', project='demo', name='handoff', expect_generation=1)
            elsewhere = self.send(server)
            self.fail_push(server, elsewhere)
            other = tempfile.TemporaryDirectory(prefix='abr-', dir='/tmp')
            self.addCleanup(other.cleanup)
            result = self.reregister(server, cwd=other.name)
            self.assertEqual((result['readdressed'], result['readdressed_ids']), (0, []))
            for mid, state in ((silent, 'pending'), (reached, 'submitted'),
                               (on_channel, 'pending'), (elsewhere, 'pending')):
                stored = server.call('show', id=mid)
                self.assertEqual((stored['state'], stored['route']['session']), (state, self.old))
            rows = self.inbox(server)
            self.assertTrue(rows[silent]['route_dropped'])
            self.assertTrue(rows[elsewhere]['route_dropped'])

    def test_the_same_directory_moves_only_the_message_with_wake_evidence(self):
        with RunningBroker() as server:
            self.start(server)
            silent = self.send(server)
            failed = self.send(server)
            self.fail_push(server, failed, wake={'state': 'failed', 'code': 'live_unreachable'})
            result = self.reregister(server)
            self.assertEqual(result['readdressed_ids'], [failed])
            self.assertEqual(server.call('show', id=silent)['route']['session'], self.old)
            self.assertTrue(self.inbox(server)[silent]['route_dropped'])

    def test_register_cli_prints_the_moved_ids_and_pushes_nothing(self):
        with RunningBroker() as server:
            self.start(server)
            mid = self.send(server)
            self.fail_push(server, mid)
            server.call('unregister', project='demo', name='claude-tower')
            out = io.StringIO()
            env = {'CLAUDE_CONFIG_DIR': server.temp.name}
            with mock.patch.dict(os.environ, env), mock.patch('sys.stdout', out):
                code = bridge.main(['--socket', server.path, 'register', 'claude-tower', '--project', 'demo',
                                    '--cwd', self.cwd,
                                    '--transport', 'claude-peer', '--session', self.new])
            self.assertEqual(code, 0)
            printed = json.loads(out.getvalue())
            self.assertEqual((printed['readdressed'], printed['readdressed_ids']), (1, [mid]))
            stored = server.call('show', id=mid)
            self.assertEqual([(e['state'], e.get('event')) for e in stored['events']][-1], ('pending', None))
            self.assertEqual(stored['state'], 'pending')


if __name__ == '__main__':
    unittest.main()
