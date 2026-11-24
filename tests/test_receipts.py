"""Focused #141 checks. All scratch is under the explicit lane/state root."""
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
import receipts

HEAD = '1' * 40
OLD = '2' * 40
SCRIPTS = Path(bridge.__file__).resolve().parent
# Git execution belongs to the parent. The subprocess harness only stubs metadata;
# capture and the public CLI parser/dispatch are exercised without stubs.
HARNESS = ('import sys; sys.path.insert(0, sys.argv.pop(1)); import bridge, receipts; '
           'receipts.git_snapshot=lambda cwd: {"head":None,"dirty":None}; '
           'raise SystemExit(bridge.main(sys.argv[1:]))')


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get('AGENT_BRIDGE_TEST_ROOT',
                    Path.home() / '.local/share/agent-bridge/bridge.state/lane-141/tests'))
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temp = tempfile.TemporaryDirectory(prefix='r-', dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = self.root / 'rows.json'
        self.state = self.root / 'state'
        self.state.mkdir()

    def fixture(self, code=0, head=HEAD, log=b'12 tests passed\n', native=False):
        if native:
            rid = 'a' * 32
            folder = self.state / 'receipts'
            folder.mkdir(exist_ok=True)
            lp = folder / (rid + '.log')
            lp.write_bytes(log); lp.chmod(0o600)
            data = dict(schema=receipts.SCHEMA, id=rid, exit_code=code, head=head,
                        log=lp.name, log_bytes=len(log), log_sha256=hashlib.sha256(log).hexdigest())
            rp = folder / (rid + '.json')
            rp.write_text(json.dumps(data)); rp.chmod(0o600)
            return rid
        rp = self.root / 'result.json'
        rp.write_text(json.dumps({'exitCode': code, 'sourceCommit': head,
                                 'root': '/nonlocal/work', 'out': '/nonlocal/evidence'}))
        rp.with_name('command.log').write_bytes(log)
        return str(rp)

    def row(self, reference, **extra):
        return dict(claim='focused tests', receipt=reference, exit=0, figures=['12 tests passed'], **extra)

    def check(self, rows, head=HEAD):
        self.rows.write_text(json.dumps(rows))
        return receipts.check(self.state, self.rows, head)

    def cli(self, *args):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            code = bridge.main([*args, '--state-dir', str(self.state)])
        return code, json.loads(out.getvalue())

    def command(self, source):
        return [sys.executable, '-c', HARNESS, str(SCRIPTS), 'run', '--state-dir', str(self.state),
                '--cwd', str(self.root), '--label', 'fixture', '--', sys.executable, '-c', source]

    def read_run(self, stderr):
        last_line = stderr.decode().splitlines()[-1]
        self.assertTrue(last_line.startswith('RUN_RECEIPT: '), last_line[-80:])
        rid = last_line.removeprefix('RUN_RECEIPT: ')
        self.assertRegex(rid, '^[a-f0-9]{32}$')
        path = self.state / 'receipts' / (rid + '.json')
        data = json.loads(path.read_text())
        log = path.with_name(data['log'])
        for p in (path, log):
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(data['log_bytes'], log.stat().st_size)
        self.assertEqual(data['log_sha256'], hashlib.sha256(log.read_bytes()).hexdigest())
        return data, log.read_bytes()

    def test_missing_receipt_names_row_and_never_sends(self):
        self.rows.write_text(json.dumps([self.row('missing.json')]))
        with mock.patch.object(bridge.broker, 'request') as send:
            code, result = self.cli('handover', '--project', 'p', '--from', 'w', '--to', 't',
                                    '--card', '#141', '--rows', str(self.rows), '--head', HEAD)
        self.assertEqual(code, 1)
        self.assertIn('HANDOVER_UNVERIFIED: row 1', result['error'])
        send.assert_not_called()

    def test_exit_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'row 1: exit mismatch'):
            self.check([self.row(self.fixture(code=7))])

    def test_invented_figure_names_second_row(self):
        row = self.row(self.fixture())
        with self.assertRaisesRegex(ValueError, 'row 2: figure not in log: "999 tests"'):
            self.check([row, {**row, 'figures': ['999 tests']}])

    def test_known_head_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'row 1: head mismatch'):
            self.check([self.row(self.fixture(head=OLD))])

    def test_stated_earlier_head_is_visible(self):
        checked = self.check([self.row(self.fixture(head=OLD), head=OLD)])
        body = receipts.message('#141', HEAD, checked)
        self.assertIn('earlier/different head: ' + OLD, body)
        self.assertIn('verified (pool)', body)

    def test_pool_reads_sibling_ignores_remote_paths(self):
        reference = self.fixture()
        data = json.loads(Path(reference).read_text())
        data['log'] = '/nonlocal/misleading.log'
        Path(reference).write_text(json.dumps(data))
        found = self.check([self.row(reference)])[0]
        self.assertEqual((found['recorded_head'], found['receipt_kind']), (HEAD, 'pool'))

    def test_empty_and_absent_pool_head_stay_unknown(self):
        for head in ('', None):
            ref = self.fixture(head=head)
            if head is None:
                data = json.loads(Path(ref).read_text()); del data['sourceCommit']
                Path(ref).write_text(json.dumps(data))
            checked = self.check([self.row(ref)])
            self.assertIsNone(checked[0]['recorded_head'])
            self.assertIn('recorded HEAD: unknown', receipts.message('#141', HEAD, checked))

    def test_unreadable_receipt_invalid_json_and_missing_pool_log(self):
        ref = self.fixture()
        Path(ref).write_text('{')
        with self.assertRaisesRegex(ValueError, 'HANDOVER_UNVERIFIED: row 1'):
            self.check([self.row(ref)])
        self.fixture()
        Path(ref).with_name('command.log').unlink()
        with self.assertRaisesRegex(ValueError, 'HANDOVER_UNVERIFIED: row 1'):
            self.check([self.row(ref)])

    def test_generic_external_relative_log_and_exit(self):
        ref = self.root / 'external.json'
        ref.write_text(json.dumps({'exit_code': 9, 'log': 'external.log'}))
        ref.with_name('external.log').write_text('nonzero 9')
        checked = self.check([dict(claim='failure', receipt=str(ref), exit=9, figures=['nonzero 9'])])
        self.assertEqual(checked[0]['receipt_kind'], 'external')
        self.assertIsNone(checked[0]['recorded_head'])

    def test_native_id_and_path_hash_size_and_mode(self):
        rid = self.fixture(native=True)
        rp = self.state / 'receipts' / (rid + '.json')
        self.check([self.row(rid)])
        self.check([self.row(str(rp))])
        lp = rp.with_suffix('.log')
        lp.write_bytes(b'12 tests passed\nX')
        with self.assertRaisesRegex(ValueError, 'log byte size mismatch'):
            self.check([self.row(rid)])
        lp.write_bytes(b'99 tests passed\n')
        with self.assertRaisesRegex(ValueError, 'log sha256 mismatch'):
            self.check([self.row(rid)])
        self.fixture(native=True); lp.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'owner-private'):
            self.check([self.row(rid)])

    def test_symlink_and_nonregular_receipts_rejected(self):
        ref = self.fixture()
        link = self.root / 'link.json'; link.symlink_to(ref)
        with self.assertRaisesRegex(ValueError, 'HANDOVER_UNVERIFIED: row 1'):
            self.check([self.row(str(link))])
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            self.check([self.row(str(fifo))])

    def test_invalid_field_types_and_heads_fail_closed(self):
        ref = self.fixture()
        for row in ({**self.row(ref), 'exit': True}, {**self.row(ref), 'figures': '12'},
                    {**self.row(ref), 'figures': ['']}, {**self.row(ref), 'head': 'short'},
                    {**self.row(ref), 'claim': ''}):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, 'row 1'):
                self.check([row])
        for data in ({'exitCode': True}, {'exitCode': '0'}, {'exitCode': 0, 'sourceCommit': 'short'}):
            Path(ref).write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, 'row 1'):
                self.check([self.row(ref)])

    def test_empty_rows_and_bad_document_head(self):
        for rows in ([], {}, 'bad'):
            with self.assertRaisesRegex(ValueError, 'HANDOVER_UNVERIFIED'):
                self.check(rows)
        with self.assertRaisesRegex(ValueError, 'head must be a full SHA'):
            self.check([self.row(self.fixture())], head='short')

    def test_native_log_path_traversal_rejected(self):
        rid = self.fixture(native=True)
        path = self.state / 'receipts' / (rid + '.json')
        data = json.loads(path.read_text()); data['log'] = '../elsewhere.log'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'invalid bridge log reference'):
            self.check([self.row(rid)])

    def test_check_cli_is_local_and_returns_verified_rows(self):
        self.check([self.row(self.fixture())])
        with mock.patch.object(bridge.broker, 'request') as request:
            code, result = self.cli('handover', 'check', '--rows', str(self.rows), '--head', HEAD)
        self.assertEqual(code, 0)
        self.assertTrue(result['rows'][0]['verified'])
        request.assert_not_called()

    def test_send_shape_note_verbatim_one_dispatch_and_stable_key(self):
        self.check([self.row(self.fixture())])
        note = self.root / 'note'; note.write_bytes('free\r\n한글 tail\n'.encode())
        sent = {'id': 'fixture-message', 'state': 'pending'}
        args = ['handover', '--project', 'p', '--from', 'w', '--to', 't', '--card', '#141',
                '--rows', str(self.rows), '--head', HEAD, '--note-file', str(note)]
        with mock.patch.object(bridge.broker, 'request', return_value=sent) as request, \
             mock.patch.object(bridge, 'push_if_peer', return_value=sent) as push:
            code, result = self.cli(*args)
        self.assertEqual((code, result), (0, sent))
        request.assert_called_once(); push.assert_called_once()
        payload = request.call_args.args[1]
        self.assertEqual((payload['op'], payload['project'], payload['sender'], payload['recipient']),
                         ('send', 'p', 'w', 't'))
        body = payload['body']
        for text in ('Card: "#141"', 'Head: "' + HEAD + '"', '| Claim | Exit | Figures | Receipt reference',
                     '12 tests passed', 'verified (pool)', 'Unverified free-text note'):
            self.assertIn(text, body)
        self.assertTrue(body.endswith(note.read_bytes().decode()))
        self.assertNotIn('12 tests passed\n', body)  # full log was not attached
        with mock.patch.object(bridge.broker, 'request', return_value=sent) as again, \
             mock.patch.object(bridge, 'push_if_peer', return_value=sent):
            self.cli(*args)
        self.assertEqual(payload['key'], again.call_args.args[1]['key'])

    def test_uncertain_send_is_never_retried(self):
        self.check([self.row(self.fixture())])
        with mock.patch.object(bridge.broker, 'request', side_effect=OSError('uncertain')) as request:
            code, result = self.cli('handover', '--project', 'p', '--from', 'w', '--to', 't',
                                    '--card', '#141', '--rows', str(self.rows), '--head', HEAD)
        self.assertEqual(code, 1)
        request.assert_called_once()

    def test_message_size_and_control_characters(self):
        row = self.row(self.fixture())
        row['claim'] = 'claim|\nnew row'
        checked = self.check([row])
        body = receipts.message('card\ntext', HEAD, checked)
        self.assertIn('claim\\u007c\\nnew row', body)
        with self.assertRaisesRegex(ValueError, 'exceeds 32768'):
            receipts.message('#141', HEAD, checked, '한' * 12000)

    def test_metadata_unknown_and_dirty_with_mocked_git_only(self):
        with mock.patch.object(receipts.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, HEAD+'\n', ''),
                subprocess.CompletedProcess([], 0, b' M file\n', b'')]):
            self.assertEqual(receipts.git_snapshot(self.root), {'head': HEAD, 'dirty': True})
        with mock.patch.object(receipts.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertEqual(receipts.git_snapshot(self.root), {'head': None, 'dirty': None})

    def test_pipe_stdin_order_binary_nonzero_and_receipt(self):
        source = ('import os,sys; '
                  'os.write(1,("TTY="+str([os.isatty(i) for i in range(3)])+"\\n").encode()); '
                  'x=input("READ:"); os.write(1,b"OUT1\\n"); os.write(2,b"ERR2\\n"); '
                  'os.write(1,b"INPUT="+x.encode()+b"\\n\\xff"); sys.exit(7)')
        done = subprocess.run(self.command(source), input=b'probe-141\n', capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 7, done.stderr)
        expected = b'TTY=[False, False, False]\nREAD:OUT1\nINPUT=probe-141\n\xff'
        self.assertEqual(done.stdout, expected)
        self.assertTrue(done.stderr.startswith(b'ERR2\n'))
        data, log = self.read_run(done.stderr)
        self.assertEqual(log.replace(b'ERR2\n', b''), expected)
        self.assertIn(b'ERR2\n', log)
        self.assertEqual(data['log_order'], 'observed-read-order')
        self.assertEqual((data['capture'], data['exit_code'], data['head'], data['dirty']),
                         ('routed', 7, None, None))
        self.assertEqual(data['cwd'], str(self.root.resolve()))
        self.assertEqual(data['argv'][-1], source)
        self.assertLessEqual(data['started'], data['finished'])

    def test_real_pty_input_order_nonzero_and_unchanged_terminal(self):
        self.assert_inherited_terminal_capture(queued=False)

    def test_tty_input_queued_before_startup_is_not_flushed(self):
        self.assert_inherited_terminal_capture(queued=True)

    def test_two_queued_tty_lines_one_line_child_leaves_second_on_original_stdin(self):
        master, slave = pty.openpty()
        child = None
        source = ('import os\nline=bytearray()\n'
                  'while not line.endswith(b"\\n"):\n line.extend(os.read(0,1))\n'
                  'os.write(1,b"INPUT="+line)\n')
        before = termios.tcgetattr(slave)
        try:
            os.write(master, b'first-141\nsecond-141\n')
            self.assertTrue(select.select([master], [], [], 2)[0])
            child = subprocess.Popen(self.command(source), stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True)
            child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0)
            self.assertTrue(select.select([slave], [], [], 1)[0], 'second queued line was consumed')
            self.assertEqual(os.read(slave, 4096), b'second-141\n')
            self.assertEqual(termios.tcgetattr(slave), before)
            output = bytearray()
            while select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
            self.assertIn(b'INPUT=first-141\r\n', output)
            rid = output.split(b'RUN_RECEIPT: ')[-1].strip().decode()
            data, log = self.read_run(('RUN_RECEIPT: '+rid+'\n').encode())
            self.assertEqual(data['capture'], 'routed')
            self.assertIn(b'INPUT=first-141\n', log)
            self.assertNotIn(b'second-141', log)
        finally:
            if child is not None and child.poll() is None:
                child.kill(); child.wait()
            os.close(master); os.close(slave)

    def test_tty_foreground_group_sigint_is_not_forwarded_twice(self):
        master, slave = pty.openpty()
        child = None
        source = ('import os,signal\ncount=0\n'
                  'def interrupt(sig,frame):\n global count\n count+=1\n'
                  ' os.write(1,("SIGNAL="+str(count)+"\\n").encode())\n'
                  'signal.signal(signal.SIGINT,interrupt)\n'
                  'print("GROUP="+str(os.getpgrp()),flush=True)\n'
                  'print("READY",flush=True)\ninput()\nprint("COUNT="+str(count),flush=True)\n')
        try:
            child = subprocess.Popen(self.command(source), stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True)
            output = bytearray()
            deadline = time.monotonic() + 10
            signaled = entered = False
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    output.extend(os.read(master, 65536))
                    if b'READY' in output and not signaled:
                        os.killpg(child.pid, signal.SIGINT); signaled = True
                    if b'SIGNAL=1' in output and not entered:
                        os.write(master, b'continue\n'); entered = True
                if child.poll() is not None:
                    while select.select([master], [], [], 0)[0]:
                        output.extend(os.read(master, 65536))
                    break
            self.assertEqual(child.wait(timeout=1), 0, bytes(output))
            self.assertIn(('GROUP='+str(child.pid)).encode(), output)
            self.assertTrue(signaled and entered)
            self.assertIn(b'COUNT=1', output)
            self.assertNotIn(b'SIGNAL=2', output)
        finally:
            if child is not None and child.poll() is None:
                child.kill(); child.wait()
            os.close(master); os.close(slave)

    def assert_inherited_terminal_capture(self, queued):
        source = ('import os,sys; print("TTY="+str([os.isatty(i) for i in range(3)]),flush=True); '
                  'x=input("READ:"); os.write(1,b"OUT1\\n"); os.write(2,b"ERR2\\n"); '
                  'print("INPUT="+x,flush=True); sys.exit(7)')
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        if queued:
            os.write(master, b'probe-141\n')
            self.assertTrue(select.select([master], [], [], 2)[0])
            self.assertIn(b'probe-141', os.read(master, 65536))
        child = subprocess.Popen(self.command(source), stdin=slave, stdout=slave, stderr=slave,
                                 start_new_session=True)
        output = bytearray()
        try:
            deadline = time.monotonic() + 10
            injected = queued
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    output.extend(os.read(master, 65536))
                    if b'READ:' in output and not injected:
                        os.write(master, b'probe-141\n'); injected = True
                if child.poll() is not None:
                    while select.select([master], [], [], 0)[0]:
                        output.extend(os.read(master, 65536))
                    break
            self.assertEqual(child.wait(timeout=1), 7, bytes(output))
            self.assertTrue(injected)
            self.assertIn(b'TTY=[True, True, True]', output)
            self.assertLess(output.index(b'OUT1'), output.index(b'ERR2'))
            self.assertIn(b'INPUT=probe-141', output)
            self.assertEqual(termios.tcgetattr(slave), before)
            rid = output.split(b'RUN_RECEIPT: ')[-1].strip().decode()
            data, log = self.read_run(('RUN_RECEIPT: '+rid+'\n').encode())
            self.assertEqual((data['capture'], data['exit_code']), ('routed', 7))
            self.assertNotIn(b'probe-141', log.splitlines())  # input echo stays on original terminal
            self.assertIn(b'OUT1\nERR2\nINPUT=probe-141', log)
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
            os.close(master); os.close(slave)

    def test_piped_stdin_with_shared_terminal_outputs_keeps_tty_and_order(self):
        master, slave = pty.openpty()
        source = ('import os,sys; print("TTY="+str([os.isatty(i) for i in range(3)]),flush=True); '
                  'x=input("READ:"); os.write(1,b"OUT1\\n"); os.write(2,b"ERR2\\n"); '
                  'print("INPUT="+x,flush=True); sys.exit(7)')
        try:
            child = subprocess.Popen(self.command(source), stdin=subprocess.PIPE, stdout=slave,
                                     stderr=slave, start_new_session=True)
            child.communicate(b'probe-141\n', timeout=10)
            output = bytearray()
            while select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
            self.assertEqual(child.returncode, 7, bytes(output))
            self.assertIn(b'TTY=[False, True, True]', output)
            self.assertIn(b'READ:OUT1\r\nERR2\r\nINPUT=probe-141\r\n', output)
            self.assertNotIn(b'\r\r\n', output)
            rid = output.split(b'RUN_RECEIPT: ')[-1].strip().decode()
            data, log = self.read_run(('RUN_RECEIPT: '+rid+'\n').encode())
            self.assertEqual((data['capture'], data['log_order']), ('routed', 'shared-pty'))
            self.assertIn(b'READ:OUT1\nERR2\nINPUT=probe-141\n', log)
        finally:
            if 'child' in locals() and child.poll() is None:
                child.kill(); child.wait()
            os.close(master); os.close(slave)

    def test_each_single_terminal_output_preserves_routes_and_tty_detection(self):
        source = ('import os,sys; print("TTY="+str([os.isatty(i) for i in range(3)]),flush=True); '
                  'x=input("READ:"); print("OUT1",flush=True); print("ERR2",file=sys.stderr,flush=True); '
                  'print("INPUT="+x,flush=True); sys.exit(7)')
        for terminal_output in (1, 2):
            with self.subTest(terminal_output=terminal_output):
                master, slave = pty.openpty()
                try:
                    child = subprocess.Popen(self.command(source), stdin=subprocess.PIPE,
                        stdout=slave if terminal_output == 1 else subprocess.PIPE,
                        stderr=slave if terminal_output == 2 else subprocess.PIPE,
                        start_new_session=True)
                    stdout, stderr = child.communicate(b'probe-141\n', timeout=10)
                    output = bytearray()
                    while select.select([master], [], [], 0.1)[0]:
                        output.extend(os.read(master, 65536))
                    self.assertEqual(child.returncode, 7, (stdout, stderr, bytes(output)))
                    if terminal_output == 1:
                        self.assertIn(b'TTY=[False, True, False]', output)
                        self.assertIn(b'READ:OUT1\r\nINPUT=probe-141\r\n', output)
                        self.assertNotIn(b'ERR2', output)
                        self.assertTrue(stderr.startswith(b'ERR2\n'))
                        data, log = self.read_run(stderr)
                    else:
                        self.assertEqual(stdout, b'TTY=[False, False, True]\nREAD:OUT1\nINPUT=probe-141\n')
                        self.assertTrue(output.startswith(b'ERR2\r\n'))
                        self.assertNotIn(b'OUT1', output)
                        rid = output.split(b'RUN_RECEIPT: ')[-1].strip().decode()
                        data, log = self.read_run(('RUN_RECEIPT: '+rid+'\n').encode())
                    self.assertEqual(data['log_order'], 'observed-read-order')
                    self.assertIn(b'ERR2\n', log)
                finally:
                    if 'child' in locals() and child.poll() is None:
                        child.kill(); child.wait()
                    os.close(master); os.close(slave)

    def test_tty_stdin_with_redirected_outputs_inherits_queued_input(self):
        master, slave = pty.openpty()
        try:
            os.write(master, b'probe-141\n')
            source = ('import os,sys; print("TTY="+str([os.isatty(i) for i in range(3)])); '
                      'x=input("READ:"); print("OUT1"); print("ERR2",file=sys.stderr); '
                      'print("INPUT="+x); sys.exit(7)')
            done = subprocess.run(self.command(source), stdin=slave,
                                  capture_output=True, timeout=10)
            self.assertEqual(done.returncode, 7, done.stderr)
            self.assertEqual(done.stdout, b'TTY=[True, False, False]\nREAD:OUT1\nINPUT=probe-141\n')
            self.assertTrue(done.stderr.startswith(b'ERR2\n'))
            data, log = self.read_run(done.stderr)
            self.assertEqual(data['capture'], 'routed')
        finally:
            os.close(master); os.close(slave)

    def test_tty_stdin_with_one_redirected_output_preserves_routes(self):
        source = ('import os,sys; print("TTY="+str([os.isatty(i) for i in range(3)]),flush=True); '
                  'x=input("READ:"); print("OUT1",flush=True); print("ERR2",file=sys.stderr,flush=True); '
                  'print("INPUT="+x,flush=True); sys.exit(7)')
        for terminal_output in (1, 2):
            with self.subTest(terminal_output=terminal_output):
                master, slave = pty.openpty()
                child = None
                try:
                    os.write(master, b'probe-141\n')
                    self.assertTrue(select.select([master], [], [], 2)[0])
                    os.read(master, 65536)  # input is queued before the recorder starts
                    child = subprocess.Popen(self.command(source), stdin=slave,
                        stdout=slave if terminal_output == 1 else subprocess.PIPE,
                        stderr=slave if terminal_output == 2 else subprocess.PIPE,
                        start_new_session=True)
                    stdout, stderr = child.communicate(timeout=10)
                    output = bytearray()
                    while select.select([master], [], [], 0.1)[0]:
                        output.extend(os.read(master, 65536))
                    self.assertEqual(child.returncode, 7, (stdout, stderr, bytes(output)))
                    if terminal_output == 1:
                        self.assertIn(b'TTY=[True, True, False]', output)
                        self.assertIn(b'OUT1\r\nINPUT=probe-141\r\n', output)
                        self.assertNotIn(b'ERR2', output)
                        # With both stdin/stdout TTY, CPython's stdio readline
                        # writes its prompt to stderr, as the unwrapped baseline does.
                        self.assertTrue(stderr.startswith(b'READ:ERR2\n'))
                        data, log = self.read_run(stderr)
                    else:
                        self.assertEqual(stdout, b'TTY=[True, False, True]\nREAD:OUT1\nINPUT=probe-141\n')
                        self.assertTrue(output.startswith(b'ERR2\r\n'))
                        self.assertNotIn(b'OUT1', output)
                        rid = output.split(b'RUN_RECEIPT: ')[-1].strip().decode()
                        data, log = self.read_run(('RUN_RECEIPT: '+rid+'\n').encode())
                    self.assertEqual(data['log_order'], 'observed-read-order')
                finally:
                    if child is not None and child.poll() is None:
                        child.kill(); child.wait()
                    os.close(master); os.close(slave)

    def test_concurrent_runs_distinct_private_receipts(self):
        def one(index):
            return subprocess.run(self.command(f'print("run {index}")'), capture_output=True, timeout=10)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            done = list(pool.map(one, range(6)))
        ids = set()
        for result in done:
            self.assertEqual(result.returncode, 0, result.stderr)
            data, log = self.read_run(result.stderr)
            ids.add(data['id'])
            self.assertEqual(log, result.stdout)
        self.assertEqual(len(ids), 6)

    def test_capture_failure_does_not_publish_receipt(self):
        with mock.patch.object(receipts, 'git_snapshot', return_value={'head': None, 'dirty': None}), \
             mock.patch.object(receipts.os, 'isatty', return_value=False), \
             mock.patch.object(receipts, 'routed_capture', side_effect=OSError('capture failure')):
            with self.assertRaisesRegex(OSError, 'capture failure'):
                receipts.run(self.state, 'fixture', self.root, ['unused'])
        self.assertEqual(list((self.state / 'receipts').glob('*.json')), [])

    def test_native_receipt_permissions_and_symlink_log_rejected(self):
        rid = self.fixture(native=True)
        rp = self.state / 'receipts' / (rid + '.json')
        rp.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'owner-private'):
            self.check([self.row(str(rp))])
        rp.chmod(0o600)
        lp = rp.with_suffix('.log'); lp.unlink()
        target = self.root / 'target'; target.write_text('12 tests passed\n'); target.chmod(0o600)
        lp.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'HANDOVER_UNVERIFIED: row 1'):
            self.check([self.row(rid)])

    def test_run_refuses_symlink_receipt_directory_before_child(self):
        target = self.root / 'elsewhere'; target.mkdir()
        (self.state / 'receipts').symlink_to(target, target_is_directory=True)
        with mock.patch.object(receipts.os, 'isatty', return_value=False), \
             mock.patch.object(receipts, 'routed_capture') as capture:
            with self.assertRaises(OSError):
                receipts.run(self.state, 'fixture', self.root, ['unused'])
        capture.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])

    def test_signal_status_is_preserved(self):
        done = subprocess.run(self.command('import os,signal; os.kill(os.getpid(),signal.SIGTERM)'),
                              capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 128 + signal.SIGTERM, done.stderr)
        data, log = self.read_run(done.stderr)
        self.assertEqual(data['exit_code'], 128 + signal.SIGTERM)

    def test_missing_executable_returns_127_and_receipt(self):
        command = self.command('unused')
        command[-3:] = [str(self.root / 'missing-executable')]
        done = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 127, done.stdout + done.stderr)
        data, log = self.read_run(done.stderr)
        self.assertEqual(data['exit_code'], 127)
        self.assertIn(b'missing-executable', log)

    def test_oversize_message_refuses_before_dispatch(self):
        self.check([self.row(self.fixture())])
        note = self.root / 'note'; note.write_text('X' * 32768)
        with mock.patch.object(bridge.broker, 'request') as request:
            code, result = self.cli('handover', '--project', 'p', '--from', 'w', '--to', 't',
                                    '--card', '#141', '--rows', str(self.rows), '--head', HEAD,
                                    '--note-file', str(note))
        self.assertEqual(code, 1)
        self.assertIn('HANDOVER_UNVERIFIED', result['error'])
        request.assert_not_called()

    def test_clean_or_failed_status_and_nonrepository_metadata(self):
        for status, dirty in ((subprocess.CompletedProcess([], 0, b'', b''), False),
                              (subprocess.CompletedProcess([], 1, b'', b'failed'), None)):
            with mock.patch.object(receipts.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, HEAD + '\n', ''), status]):
                self.assertEqual(receipts.git_snapshot(self.root), {'head': HEAD, 'dirty': dirty})
        with mock.patch.object(receipts.subprocess, 'run', return_value=
                               subprocess.CompletedProcess([], 128, '', 'not a repository')):
            self.assertEqual(receipts.git_snapshot(self.root), {'head': None, 'dirty': None})

    def test_log_write_order_under_volume(self):
        source = ('import os; '
                  '[(os.write(1,b"A"*4096),os.write(2,b"B"*4096)) for _ in range(32)]')
        done = subprocess.run(self.command(source), capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, b'A' * (4096 * 32))
        self.assertTrue(done.stderr.startswith(b'B' * (4096 * 32)))
        data, log = self.read_run(done.stderr)
        self.assertEqual(log.count(b'A'), 4096 * 32)
        self.assertEqual(log.count(b'B'), 4096 * 32)
        self.assertEqual(data['log_order'], 'observed-read-order')


if __name__ == '__main__':
    unittest.main()
