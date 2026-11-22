"""Issue #120: a broker restart keeps claude-peer routes to idle-closed Claude Desktop sessions."""
from contextlib import redirect_stderr
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
import bridge  # noqa: F401  (puts the skill scripts on sys.path, like the other tests)
import route_store
from test_peer import FakeClaude

HOST = 'local_11111111-2222-3333-4444-555555555555'


class ClaudePeerRestoreTests(unittest.TestCase):
    def setUp(self):
        # Short /tmp path: the fake inbox is an AF_UNIX socket under this root.
        self.temp = tempfile.TemporaryDirectory(prefix='abr-', dir='/tmp')
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'work'
        self.workspace.mkdir()
        self.desktop = self.root / 'desktop' / 'acct' / 'org'
        self.desktop.mkdir(parents=True)
        self.sid = str(uuid.uuid4())
        self.fakes = []
        env = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root),
                                           'CLAUDE_DESKTOP_SESSIONS_DIR': str(self.root / 'desktop')})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        for fake in self.fakes:
            fake.close()
        self.temp.cleanup()

    def route(self, **changes):
        return {'project': 'demo', 'name': 'alpha', 'cwd': str(self.workspace), 'session': self.sid,
                'transport': 'claude-peer', 'executable': None, **changes}

    def metadata(self, sid=None, cwd=None, **extra):
        data = {'sessionId': HOST, 'cliSessionId': sid or self.sid,
                'cwd': str(cwd or self.workspace), 'isArchived': False, **extra}
        (self.desktop / f'{HOST}.json').write_text(json.dumps(data, indent=2))

    def restore(self):
        state = self.root / 'state'
        store = route_store.RouteStore(state)
        self.addCleanup(store.close)
        snapshot = {'version': 1, 'endpoints': [self.route()], 'channels': []}
        store.path.write_text(json.dumps(snapshot))
        os.chmod(store.path, 0o600)
        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic):
            endpoints, _ = store.load()
        lines = [json.loads(line) for line in diagnostic.getvalue().splitlines()]
        return endpoints, lines

    def test_not_live_session_with_saved_desktop_metadata_is_retained(self):
        self.metadata()
        endpoints, lines = self.restore()
        self.assertEqual(endpoints[('demo', 'alpha')]['session'], self.sid)
        self.assertEqual(lines, [dict(routing_retain='endpoint', index=0, project='demo', name='alpha',
                                      reason=route_store.CLAUDE_NOT_LIVE_RETAINED)])
        self.assertEqual(route_store.CLAUDE_NOT_LIVE_RETAINED,
                         'Claude session not live; saved Desktop session retained for wake')

    def test_scheduled_task_metadata_is_still_a_saved_session(self):
        # The wake reports scheduled-task sessions as unsupported; the route itself still exists.
        self.metadata(scheduledTaskId='task-1')
        self.assertEqual(route_store.verify_route(self.route()), route_store.CLAUDE_NOT_LIVE_RETAINED)

    def assert_dropped(self, reason):
        endpoints, lines = self.restore()
        self.assertEqual(endpoints, {})
        self.assertEqual(lines, [dict(routing_drop='endpoint', index=0, reason=reason,
                                      project='demo', name='alpha')])

    def test_archived_desktop_session_is_dropped(self):
        self.metadata(isArchived=True)
        self.assert_dropped('Claude Desktop session archived')

    def test_missing_desktop_metadata_is_dropped(self):
        self.assert_dropped('Claude session not live and no saved Desktop session')

    def test_missing_desktop_directory_is_dropped(self):
        os.environ['CLAUDE_DESKTOP_SESSIONS_DIR'] = str(self.root / 'absent')
        self.assert_dropped('Claude session not live and no saved Desktop session')

    def test_metadata_for_another_session_id_is_dropped(self):
        self.metadata(sid=str(uuid.uuid4()))
        self.assert_dropped('Claude session not live and no saved Desktop session')

    def test_metadata_for_another_workspace_is_dropped(self):
        other = self.root / 'other'
        other.mkdir()
        self.metadata(cwd=other)
        self.assert_dropped('session workspace differs')

    def test_metadata_without_workspace_is_dropped(self):
        data = {'sessionId': HOST, 'cliSessionId': self.sid, 'isArchived': False}
        (self.desktop / f'{HOST}.json').write_text(json.dumps(data))
        self.assert_dropped('session workspace differs')

    def test_live_peer_keeps_its_checks(self):
        self.fakes.append(FakeClaude(self.root, self.sid))
        # FakeClaude records the root as its cwd; the live check compares it with the route.
        self.assertIsNone(route_store.verify_route(self.route(cwd=str(self.root))))
        with self.assertRaisesRegex(ValueError, 'Session or workspace differs'):
            route_store.verify_route(self.route())

    def test_live_session_without_inbox_is_dropped_even_with_saved_metadata(self):
        # Running but unreachable: a wake cannot restart it, so the old drop stands.
        self.metadata(cwd=self.root)
        self.fakes.append(FakeClaude(self.root, self.sid, listen=False))
        record = self.root / 'sessions' / f'{os.getpid()}.json'
        data = json.loads(record.read_text())
        data.pop('messagingSocketPath')
        record.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'Peer socket unavailable'):
            route_store.verify_route(self.route(cwd=str(self.root)))

    def test_live_peer_with_foreign_socket_file_is_dropped(self):
        self.fakes.append(FakeClaude(self.root, self.sid, listen=False))
        Path(self.fakes[0].sock).write_text('not a socket')
        with self.assertRaisesRegex(ValueError, 'Peer socket unavailable'):
            route_store.verify_route(self.route(cwd=str(self.root)))


if __name__ == '__main__':
    unittest.main()
