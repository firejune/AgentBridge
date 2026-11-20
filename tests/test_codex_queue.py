"""The Desktop queue's own checks, run against fakes: no process, store or app is read."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import codex_preview
import codex_queue
import codex_wake_open
import delivery
from codex_live import ThreadArchived

UID = str(os.getuid())
HELPER = (str(codex_queue.APP) + '/Contents/Frameworks/Codex Framework.framework/Versions/158.0/'
          'Helpers/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)')
SERVER = f'{codex_queue.CLI} app-server --analytics-default-enabled'
OVERRIDES = ('CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_EXEC_SERVER_URL', 'AGENT_BRIDGE_CODEX_IPC')


def processes(pids='4242\n', server=None, parent=None):
    """A fake `read` for lsof and the two ps calls desktop_queue_owner makes."""
    def read(argv):
        if argv[0].endswith('lsof'):
            return pids
        if argv[3] == '4242':
            return server if server is not None else f'  {UID}  77 {SERVER}\n'
        return parent if parent is not None else f'  {UID} {HELPER}\n'
    return read


class QueueOwnerTests(unittest.TestCase):
    def test_one_bundled_app_server_under_the_desktop_helper_is_the_owner(self):
        self.assertEqual(codex_queue.desktop_queue_owner('queue', processes()), ('4242', '77', HELPER))
        # lsof repeats a pid once per open descriptor.
        self.assertEqual(codex_queue.desktop_queue_owner('queue', processes('4242\n4242\n')),
                         ('4242', '77', HELPER))

    def test_anything_else_holding_or_shaping_the_queue_is_refused(self):
        other = str(os.getuid() + 1)
        cases = {
            'two holders': processes('4242\n4243\n'),
            'no holder': processes(''),
            'another user': processes(server=f'{other} 77 {SERVER}\n'),
            'another binary': processes(server=f'{UID} 77 /usr/local/bin/codex app-server\n'),
            'not an app-server': processes(server=f'{UID} 77 {codex_queue.CLI} exec\n'),
            'listening server': processes(server=f'{UID} 77 {SERVER} --listen stdio://\n'),
            'listening server, joined flag': processes(server=f'{UID} 77 {SERVER} --listen=ws://x\n'),
            'parent of another user': processes(parent=f'{other} {HELPER}\n'),
            'parent outside the app': processes(parent=f'{UID} /tmp/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)\n'),
            'parent is not the helper': processes(parent=f'{UID} {codex_queue.APP}/Contents/Frameworks/Codex Framework.framework/Versions/1/Helpers/Other\n'),
        }
        for name, read in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(codex_queue.desktop_queue_owner('queue', read))


class QueueNoticeTests(unittest.TestCase):
    def test_the_notice_states_the_scope_every_peer_envelope_states_and_grants_nothing(self):
        message = {'id': str(uuid.uuid4()), 'sender': 'a', 'recipient': 'b', 'project': 'p', 'body': 'PEER BODY',
                   'reply_to': None, 'sender_channel': None, 'channel': None}
        self.assertIn(delivery.PEER_SCOPE, delivery.envelope(message))
        self.assertIn('not a new human instruction', delivery.PEER_SCOPE)
        self.assertIn('existing user-authorized scope', delivery.PEER_SCOPE)
        notice = codex_queue.notice(message['id'])
        self.assertIn(delivery.PEER_SCOPE, notice)
        self.assertTrue(notice.endswith('bridge.py receive-codex ' + message['id']))
        self.assertNotIn(message['body'], notice)
        # A notice that scopes the turn to retrieval leaves the correspondence read and unanswered.
        self.assertNotIn('notification only', notice)


class DefaultDesktopFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name).resolve()
        self.state = Path.home() / '.codex' / codex_preview.STATE_FILE
        self.queue = Path.home() / '.codex' / 'queue_1.sqlite'
        self.signed, self.owned = [], []
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in OVERRIDES:
            os.environ.pop(name, None)

        def checked(argv, timeout=2):
            self.assertEqual(argv[:3], ['/usr/bin/codesign', '--verify', '--strict'])
            self.signed.append((argv[3], argv[4]))
            return ''

        for target, name, value in (
                (codex_queue, 'checked_output', checked),
                (codex_preview, 'state_path', lambda executable: self.state),
                (codex_preview, 'require_owned_store', self.owned.append),
                (codex_queue, 'desktop_queue_owner', lambda queue, read=None: ('4242', '77', HELPER))):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class QueueRouteTests(DefaultDesktopFixture):
    def setUp(self):
        super().setUp()
        self.route = {'transport': 'codex', 'session': str(uuid.uuid4()), 'cwd': str(self.cwd)}
        self.saved = (str(self.cwd), 0)
        patcher = patch.object(codex_preview, '_saved_thread_row', lambda route, path=None: self.saved)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_signed_store_with_the_saved_workspace_and_one_owner_passes(self):
        codex_queue.check_route(self.route)
        self.assertEqual(self.signed, [('-R=' + codex_queue.SIGNATURE, str(codex_queue.CLI_BUNDLE))])
        self.assertEqual(self.owned, [self.queue])

    def test_any_store_override_is_refused_before_a_process_runs(self):
        for name in OVERRIDES:
            with self.subTest(override=name), patch.dict(os.environ, {name: 'x'}):
                with self.assertRaises(ValueError):
                    codex_queue.check_route(self.route)
        self.assertEqual((self.signed, self.owned), ([], []))

    def test_each_remaining_refusal(self):
        with patch.object(codex_queue, 'checked_output',
                          side_effect=subprocess.CalledProcessError(1, 'codesign')):
            with self.assertRaises(subprocess.CalledProcessError):
                codex_queue.check_route(self.route)
        with patch.object(codex_preview, 'state_path', lambda executable: self.cwd / 'elsewhere.sqlite'):
            with self.assertRaisesRegex(ValueError, 'non-default state store'):
                codex_queue.check_route(self.route)
        with self.assertRaisesRegex(ValueError, 'non-default route executable'):
            codex_queue.check_route(dict(self.route, executable='/opt/other/codex'))
        self.saved = (str(self.cwd / 'other'), 0)
        with self.assertRaisesRegex(ValueError, 'workspace differs'):
            codex_queue.check_route(self.route)
        self.saved = (str(self.cwd), 1)
        with self.assertRaises(ThreadArchived):
            codex_queue.check_route(self.route)
        self.saved = (str(self.cwd), 0)
        with patch.object(codex_queue, 'desktop_queue_owner', lambda queue, read=None: None):
            with self.assertRaisesRegex(ValueError, 'no unique local Desktop queue owner'):
                codex_queue.check_route(self.route)


class WakePreflightTests(DefaultDesktopFixture):
    def setUp(self):
        super().setUp()
        self.app = self.cwd / 'Fake.app'
        (self.app / 'Contents').mkdir(parents=True)
        self.write_build(*codex_wake_open.APP_BUILD)
        patcher = patch.object(codex_queue, 'APP', self.app)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_build(self, version, build):
        with (self.app / 'Contents/Info.plist').open('wb') as output:
            plistlib.dump({'CFBundleShortVersionString': version, 'CFBundleVersion': build}, output)

    def test_verified_build_signatures_and_stores_return_the_owner(self):
        self.assertEqual(codex_wake_open.prepare(30),
                         codex_wake_open.Desktop(self.state, self.queue, ('4242', '77', HELPER)))
        self.assertEqual(self.signed, [('-R=' + codex_wake_open.APP_SIGNATURE, str(self.app)),
                                       ('-R=' + codex_queue.SIGNATURE, str(codex_queue.CLI_BUNDLE))])
        self.assertEqual(self.owned, [self.state, self.queue])

    def test_an_unverified_app_build_is_named_before_any_signature_check(self):
        self.write_build('99.0.1', '1')
        with self.assertRaisesRegex(codex_wake_open.WakeError, 'build has not been verified for queue wake'):
            codex_wake_open.prepare(30)
        self.assertEqual(self.signed, [])

    def test_overrides_bad_signatures_other_stores_and_missing_owners_share_the_context_refusal(self):
        refusals = []
        for name in OVERRIDES:
            with patch.dict(os.environ, {name: 'x'}):
                refusals.append(self.refusal())
        self.assertEqual((self.signed, self.owned), ([], []))
        with patch.object(codex_queue, 'checked_output', side_effect=subprocess.CalledProcessError(1, 'codesign')):
            refusals.append(self.refusal())
        with patch.object(codex_preview, 'state_path', lambda executable: self.cwd / 'elsewhere.sqlite'):
            refusals.append(self.refusal())
        with patch.object(codex_queue, 'desktop_queue_owner', lambda queue, read=None: None):
            refusals.append(self.refusal())
        refusals.append(self.refusal(timeout=codex_preview.CONFIG_TIMEOUT / 2))
        self.assertEqual(set(refusals), {codex_wake_open.CONTEXT_ERROR})

    def refusal(self, timeout=30):
        with self.assertRaises(codex_wake_open.WakeError) as raised:
            codex_wake_open.prepare(timeout)
        return str(raised.exception)


if __name__ == '__main__':
    unittest.main()
