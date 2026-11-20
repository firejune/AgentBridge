from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import broker
from support import RunningBroker


def run(*argv):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = bridge.main(list(argv))
    return code, json.loads(stdout.getvalue())


class BuildSkewTests(unittest.TestCase):
    def test_build_names_the_source_and_moves_with_any_module(self):
        with tempfile.TemporaryDirectory() as root:
            for name, text in (('a.py', 'A'), ('b.py', 'B'), ('notes.txt', 'ignored')):
                Path(root, name).write_text(text)
            first = broker.build_id(root)
            self.assertRegex(first, r'^[0-9a-f]{12}$')
            Path(root, 'notes.txt').write_text('changed')
            self.assertEqual(broker.build_id(root), first)
            Path(root, 'b.py').write_text('B2')
            self.assertNotEqual(broker.build_id(root), first)
            Path(root, 'b.py').write_text('B')
            Path(root, 'c.py').write_text('')
            self.assertNotEqual(broker.build_id(root), first)

    def test_same_build_pings_clean_and_a_different_cli_is_named(self):
        with RunningBroker() as server:
            code, same = run('--socket', server.path, 'ping')
            self.assertEqual((code, same['build'], same['cli_build']), (0, broker.build_id(), broker.build_id()))
            self.assertNotIn('warning', same)
            with patch.object(bridge.broker, 'build_id', return_value='0123456789ab'):
                code, skewed = run('--socket', server.path, 'ping')
            # Exit stays 0: `ping || serve` must not start a second broker over a skew.
            self.assertEqual(code, 0)
            self.assertEqual(skewed['warning'], 'BROKER_BUILD_SKEW: the running broker is build '
                             f"{same['build']} and this CLI is build 0123456789ab. "
                             'Bring both to the same build before trusting a failure.')

    def test_broker_without_a_build_is_named_as_older(self):
        with patch.object(bridge.broker, 'request', return_value={'instance': 'x', 'messages': 0}):
            code, old = run('--socket', 'test.sock', 'ping')
        self.assertEqual(code, 0)
        self.assertEqual(old['warning'], 'BROKER_BUILD_SKEW: the running broker predates build reporting '
                         f'and this CLI is build {broker.build_id()}. '
                         'Bring both to the same build before trusting a failure.')

    def test_only_an_unknown_operation_earns_the_follow_up_read(self):
        mid, calls = str(uuid.uuid4()), []

        def older(path, fields, **kwargs):
            calls.append(fields['op'])
            if fields['op'] == 'ping':
                return {'instance': 'x', 'build': 'aaaaaaaaaaaa'}
            raise ValueError(refusal)

        for refusal, expected in (('Unknown operation', ['show', 'ping']),
                                  (broker.UNKNOWN_MESSAGE_ERROR, ['show'])):
            calls.clear()
            with self.subTest(refusal=refusal), patch.object(bridge.broker, 'request', side_effect=older):
                code, result = run('--socket', 'test.sock', 'show', mid)
                self.assertEqual((code, result['error'], calls), (1, refusal, expected))
                if refusal == 'Unknown operation':
                    self.assertIn('the running broker is build aaaaaaaaaaaa and this CLI is build '
                                  + broker.build_id(), result['warning'])
                else:
                    self.assertNotIn('warning', result)

    def test_a_same_build_broker_refusing_an_operation_adds_no_claim(self):
        with RunningBroker() as server:
            with self.assertRaisesRegex(ValueError, 'Unknown operation'):
                server.call('no-such-operation')
            self.assertEqual(bridge.with_skew(SimpleNamespace(socket=server.path), {'error': 'x'},
                                              ValueError('Unknown operation')), {'error': 'x'})


if __name__ == '__main__':
    unittest.main()
