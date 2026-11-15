import concurrent.futures
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bridge
import retros
from support import RunningBroker


def cli(server, *args):
    out = io.StringIO()
    with mock.patch('sys.stdout', out):
        code = bridge.main(['--socket', server.path, *args])
    return code, json.loads(out.getvalue())


class RetroTests(unittest.TestCase):
    def test_add_filters_tally_override_and_verbatim(self):
        with RunningBroker() as server:
            root = Path(server.temp.name)
            text = root / 'retro.txt'
            text.write_bytes('First line\r\n한글 Second line\r\n'.encode('utf-8'))
            store = root / 'ledger.jsonl'
            for urgency in ('stop', 'repeat', 'note'):
                code, receipt = cli(server, 'retro', 'add', '--project', 'p', '--runner', 'w',
                    '--model', 'fixture', '--session', 'fixture-session', '--card', '#139', '--tags', 'a,b,a',
                    '--urgency', urgency, '--from-file', str(text), '--store', str(store))
                self.assertEqual(code, 0, receipt)
            found = retros.rows(store, 'p')
            self.assertEqual(len(found), 3)
            self.assertEqual(found[0]['text'], text.read_bytes().decode('utf-8'))
            self.assertEqual(found[0]['session'], 'fixture-session')
            self.assertEqual(store.stat().st_mode & 0o777, 0o600)
            code, counts = cli(server, 'retro', 'tally', '--project', 'p', '--store', str(store), '--json')
            self.assertEqual((code, counts[0]['count'], counts[0]['cards']), (0, 3, ['#139']))
            code, found = cli(server, 'retro', 'list', '--project', 'p', '--store', str(store), '--json',
                              '--tag', 'b', '--urgency', 'repeat', '--since', '2026-01-01')
            self.assertEqual((code, len(found), found[0]['urgency']), (0, 1, 'repeat'))
            self.assertEqual(retros.rows(store, 'other'), [])
            self.assertEqual(retros.rows(store, 'p', since='9999-01-01'), [])
            with mock.patch.dict(os.environ, {'AGENT_BRIDGE_RETRO_STORE': str(store)}):
                self.assertEqual(retros.store_path(root, 'p'), store)
                self.assertEqual(retros.store_path(root, 'p', str(text)), text)
            with self.assertRaisesRegex(ValueError, 'RETRO_PROJECT_INVALID'):
                retros.store_path(root, '../escape')

    def test_close_requirement_is_shared_before_all_provider_actions(self):
        with RunningBroker() as server:
            root = Path(server.temp.name)
            text = root / 'retro.txt'
            text.write_text('  \n')
            for transport in ('codex', 'agy', 'claude-peer', 'realtime', 'claude'):
                server.call('register', project='p', name='w', cwd=str(root), transport=transport,
                            session='00000000-0000-4000-8000-000000000139')
                with mock.patch.object(bridge, 'close_session') as close, mock.patch.object(bridge.models, 'read') as read:
                    for extra, error in (([], 'RETRO_REQUIRED'), (['--retro-file', str(text)], 'RETRO_EMPTY'),
                                         (['--no-retro', ' '], 'RETRO_REASON_EMPTY')):
                        code, result = cli(server, 'close', 'w', '--project', 'p', '--state-dir', server.state_dir, *extra)
                        self.assertEqual(code, 1)
                        self.assertIn(error, result['error'])
                    close.assert_not_called()
                    read.assert_not_called()
                self.assertEqual(len(server.call('routes', project='p')), 1)
                server.call('unregister', project='p', name='w')
            self.assertFalse((Path(server.state_dir) / 'retros').exists())

    def test_close_retro_and_opt_out_keep_route_and_model(self):
        with RunningBroker() as server:
            root = Path(server.temp.name)
            text = root / 'retro.txt'
            text.write_text('Evidence\n')
            server.call('register', project='p', name='w', cwd=str(root), transport='codex', session='00000000-0000-4000-8000-000000000139')
            with mock.patch.object(bridge.models, 'read', return_value={'model':'fixture-model'}):
                code, result = cli(server, 'close', 'w', '--project', 'p', '--retro-file', str(text),
                                  '--state-dir', server.state_dir, '--keep-route', '--card', '#139')
            self.assertEqual((code, result['closed'], result['route_removed']), (0, True, False))
            code, result = cli(server, 'close', 'w', '--project', 'p', '--no-retro', 'tower session',
                              '--state-dir', server.state_dir)
            self.assertEqual((code, result['route_removed']), (0, True))
            found = retros.rows(Path(server.state_dir) / 'retros/p.jsonl', 'p')
            self.assertEqual(len(found), 2)
            self.assertEqual((found[0]['runner'], found[0]['model'], found[0]['session']),
                             ('w', 'fixture-model', '00000000-0000-4000-8000-000000000139'))
            self.assertEqual((found[1]['text'], found[1]['no_retro']), (None, 'tower session'))

    def test_notifications_explicit_default_note_and_no_extra_text(self):
        with RunningBroker() as server:
            root = Path(server.temp.name)
            for name in ('w', 'hq', 'other'):
                server.call('register', project='p', name=name, cwd=str(root))
            text = root / 'retro.txt'
            text.write_text('one\ntwo\nthree\nfour\nfive\nPRIVATE TAIL\n')
            (Path(server.state_dir) / 'policy.json').write_text(json.dumps({'retro_notify':{'p':'hq'}}))
            for urgency in ('stop', 'repeat', 'note'):
                code, result = cli(server, 'retro', 'add', '--project', 'p', '--runner', 'w', '--model', 'fixture',
                    '--card', '#139', '--tags', 'a', '--urgency', urgency, '--from-file', str(text),
                    '--state-dir', server.state_dir)
                self.assertEqual(code, 0, result)
                self.assertEqual(result['notification'] is None, urgency == 'note')
            inbox = server.call('inbox', project='p', name='hq')
            messages = inbox
            self.assertEqual(len(messages), 2)
            self.assertTrue(all('PRIVATE TAIL' not in server.call('show', id=m['id'])['body'] for m in messages))
            self.assertEqual(server.call('inbox', project='p', name='other'), [])
            self.assertEqual(retros.notify_route(server.state_dir, 'p', 'other'), 'other')

    def test_notification_failure_is_recorded_once_and_does_not_block_close(self):
        with RunningBroker() as server:
            root = Path(server.temp.name)
            for name in ('w', 'hq'):
                server.call('register', project='p', name=name, cwd=str(root))
            text = root / 'retro.txt'
            text.write_text('Repeated failure')
            with mock.patch.object(bridge, 'push_if_peer', side_effect=ValueError('uncertain fixture')) as push:
                code, result = cli(server, 'close', 'w', '--project', 'p', '--retro-file', str(text),
                    '--state-dir', server.state_dir, '--notify', 'hq', '--urgency', 'repeat')
            self.assertEqual((code, result['route_removed']), (0, True))
            self.assertFalse(result['notification']['retry'])
            self.assertIn('uncertain fixture', result['notification']['error'])
            push.assert_called_once()
            self.assertEqual(len(retros.rows(Path(server.state_dir) / 'retros/p.jsonl', 'p')), 1)
            self.assertEqual(len(server.call('inbox', project='p', name='hq')), 1)

    def test_missing_notification_route_refuses_before_append_or_close(self):
        with RunningBroker() as server:
            root = Path(server.temp.name)
            server.call('register', project='p', name='w', cwd=str(root))
            text = root / 'retro.txt'
            text.write_text('Stop')
            with mock.patch.object(bridge, 'close_session') as close:
                code, result = cli(server, 'close', 'w', '--project', 'p', '--retro-file', str(text),
                    '--state-dir', server.state_dir, '--notify', 'missing', '--urgency', 'stop')
            self.assertEqual(code, 1)
            self.assertIn('RETRO_NOTIFY_ROUTE_MISSING', result['error'])
            close.assert_not_called()
            self.assertFalse((Path(server.state_dir) / 'retros').exists())

    def test_concurrent_closes_do_not_lose_or_interleave_lines(self):
        with RunningBroker() as server:
            root = Path(server.temp.name)
            import subprocess
            import sys
            for index in range(12):
                server.call('register', project='p', name=f'w{index}', cwd=str(root))
            def close(index):
                return subprocess.run([sys.executable, bridge.__file__, '--socket', server.path, 'close',
                    f'w{index}', '--project', 'p', '--state-dir', server.state_dir,
                    '--no-retro', 'scratch concurrency ' + str(index)], capture_output=True, text=True)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(close, range(12)))
            self.assertTrue(all(r.returncode == 0 for r in results), [r.stdout for r in results])
            found = retros.rows(Path(server.state_dir) / 'retros/p.jsonl', 'p')
            self.assertEqual(len(found), 12)
            self.assertEqual(len({r['runner'] for r in found}), 12)
