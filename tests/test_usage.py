"""Issue #7 (Codex sampling only). Stub app-servers; never the real CLI or broker."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import bridge
import codex_live
import codex_new
import usage
from support import RunningBroker

T0 = datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc)
RESET = int((T0 + timedelta(days=3)).timestamp())
SECRET_ACCOUNT = 'acct-should-never-be-stored'


def response(used=40, resets=RESET, allowed=True, credits=1, secondary=None):
    bucket = dict(limitId='codex', limitName=None, planType='secret-plan',
                  primary=dict(usedPercent=used, windowDurationMins=10080, resetsAt=resets),
                  secondary=secondary)
    return dict(ordinaryUsageAllowed=allowed, rateLimits=bucket,
                rateLimitsByLimitId={'codex': bucket}, accountId=SECRET_ACCOUNT,
                rateLimitResetCredits=dict(availableCount=credits, credits=[]))


class StubCodex:
    """A fake `codex app-server --stdio` that logs every request and refuses anything but reads."""
    def __init__(self, root, behavior='ok', result=None):
        self.root = Path(root)
        self.log = self.root / 'requests.jsonl'
        self.path = self.root / 'codex'
        self.path.write_text('#!' + sys.executable + '\n' + f'''
import json, sys
from pathlib import Path
assert sys.argv[1:] == ['app-server', '--stdio'], sys.argv
result = {json.dumps(result if result is not None else response())!r}
for line in sys.stdin:
    request = json.loads(line)
    with Path({str(self.log)!r}).open('a') as log:
        log.write(json.dumps(request) + '\\n')
    if 'id' not in request:
        continue
    method = request['method']
    if method == 'initialize':
        reply = {{'id': request['id'], 'result': {{'userAgent': 'stub'}}}}
    elif method == 'account/rateLimits/read' and {behavior!r} == 'error':
        reply = {{'id': request['id'], 'error': {{'message': 'token {SECRET_ACCOUNT} expired'}}}}
    elif method == 'account/rateLimits/read':
        reply = {{'id': request['id'], 'result': json.loads(result)}}
    else:
        print('Unexpected method ' + method, file=sys.stderr); sys.exit(3)
    print(json.dumps({{'method': 'account/rateLimits/updated', 'params': {{}}}}))
    print(json.dumps(reply), flush=True)
''')
        self.path.chmod(0o700)

    def methods(self):
        if not self.log.exists():
            return []
        return [json.loads(line)['method'] for line in self.log.read_text().splitlines()]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-usage-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.store = usage.UsageStore(self.root / 'state' / 'usage')

    def tearDown(self):
        self.temp.cleanup()

    def add(self, moment, used=40, resets=RESET, **fields):
        return usage.sample(self.store, lambda: response(used, resets, **fields), lambda: moment)

    def summary(self, now, since='6h'):
        return usage.summarize(self.store.load(), now, usage.parse_since(since))


class CollectionTests(Base):
    def test_stub_success_reads_only_and_stores_no_identity(self):
        stub = StubCodex(self.root)
        records = usage.sample(self.store, lambda: usage.read_codex(str(stub.path)), lambda: T0)
        self.assertEqual(stub.methods(), list(usage.READ_METHODS))
        self.assertEqual(len(records), 1)
        row = self.store.load()[0]
        self.assertEqual((row['limit_id'], row['slot'], row['used_percent'], row['window_mins']),
                         ('codex', 'primary', 40, 10080))
        self.assertEqual((row['allowed'], row['reset_credits'], row['status']), (True, 1, 'ok'))
        text = self.store.path.read_text()
        self.assertNotIn(SECRET_ACCOUNT, text)
        self.assertNotIn('secret-plan', text)

    def test_secondary_window_is_its_own_bucket(self):
        self.add(T0, secondary=dict(usedPercent=7, windowDurationMins=300, resetsAt=RESET - 3600))
        self.assertEqual([(r['slot'], r['used_percent']) for r in self.store.load()],
                         [('primary', 40), ('secondary', 7)])

    def test_failure_is_unknown_never_zero(self):
        stub = StubCodex(self.root, behavior='error')
        usage.sample(self.store, lambda: usage.read_codex(str(stub.path)), lambda: T0)
        usage.sample(self.store, lambda: usage.read_codex(str(self.root / 'missing')), lambda: T0 + timedelta(hours=2))
        rows = self.store.load()
        self.assertEqual([r['status'] for r in rows], ['failed', 'failed'])
        for row in rows:
            self.assertIsNone(row['used_percent'])
            self.assertIsNone(row['resets_at'])
        self.assertEqual(rows[1]['reason'], 'codex executable not found')
        self.assertNotIn(SECRET_ACCOUNT, self.store.path.read_text())
        self.assertIn('"used_percent": null', self.store.path.read_text())
        text = usage.format_summary(self.summary(T0 + timedelta(hours=2)), T0 + timedelta(hours=2))
        self.assertIn('unknown', text)
        self.assertNotIn('100% left', text)

    def test_failure_after_success_marks_values_unknown_freshness(self):
        self.add(T0)
        usage.sample(self.store, lambda: (_ for _ in ()).throw(OSError('down')), lambda: T0 + timedelta(minutes=15))
        row = self.summary(T0 + timedelta(minutes=16))['buckets'][0]
        self.assertEqual(row['freshness'], 'failed')

    def test_deduplication_within_the_hour(self):
        self.add(T0)
        self.add(T0 + timedelta(minutes=15))
        self.add(T0 + timedelta(minutes=45))
        self.assertEqual(len(self.store.load()), 1)
        self.add(T0 + timedelta(minutes=50), used=41)
        self.assertEqual(len(self.store.load()), 2)
        self.add(T0 + timedelta(minutes=50 + 61), used=41)
        self.assertEqual(len(self.store.load()), 3)

    def test_retention_and_size_caps(self):
        self.add(T0 - timedelta(days=31), used=1)
        self.add(T0 - timedelta(days=29), used=2)
        self.add(T0, used=3)
        self.assertEqual([r['used_percent'] for r in self.store.load()], [2, 3])
        small = usage.UsageStore(self.store.directory, max_bytes=1200)
        for i in range(20):
            usage.sample(small, lambda i=i: response(used=i), lambda i=i: T0 + timedelta(minutes=i))
        self.assertLessEqual(self.store.path.stat().st_size, 1200)
        kept = [r['used_percent'] for r in small.load()]
        self.assertEqual(kept[-1], 19)
        self.assertGreater(kept[0], 0)
        self.assertEqual(kept, sorted(kept))

    def test_atomic_private_files(self):
        self.add(T0)
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.store.directory.iterdir()], ['codex.jsonl'])
        with patch('usage.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.add(T0 + timedelta(hours=2), used=50)
        self.assertEqual([r['used_percent'] for r in self.store.load()], [40])
        self.assertEqual([p.name for p in self.store.directory.iterdir()], ['codex.jsonl'])

    def test_utc_on_disk(self):
        self.add(T0.astimezone(timezone(timedelta(hours=9))))
        row = json.loads(self.store.path.read_text())
        self.assertTrue(row['observed_at'].endswith('+00:00'))
        self.assertEqual(row['observed_at'], T0.isoformat())
        self.assertEqual(row['resets_at'], datetime.fromtimestamp(RESET, timezone.utc).isoformat())

    def test_past_reset_is_stale(self):
        self.add(T0, resets=int((T0 - timedelta(minutes=1)).timestamp()))
        self.assertEqual(self.store.load()[0]['status'], 'stale')
        self.assertEqual(self.summary(T0)['buckets'][0]['freshness'], 'stale')


class NoTurnTests(Base):
    def test_collection_cycle_starts_no_thread_turn_or_delivery(self):
        stub = StubCodex(self.root)
        forbidden = AssertionError('collection must not deliver or create')
        with patch.object(codex_live, 'deliver', side_effect=forbidden), \
                patch.object(codex_live, 'Client', side_effect=forbidden), \
                patch.object(codex_new, 'create', side_effect=forbidden, create=True), \
                patch.object(bridge, 'push_codex', side_effect=forbidden), \
                patch.object(bridge, 'push', side_effect=forbidden), \
                patch.object(bridge, 'push_agy', side_effect=forbidden):
            collector = usage.Collector(self.store, 5, str(stub.path), clock=lambda: T0)
            collector.cycle()
        self.assertEqual(stub.methods(), ['initialize', 'initialized', 'account/rateLimits/read'])
        self.assertFalse(any('thread' in m or 'turn' in m for m in stub.methods()))
        self.assertEqual(self.store.load()[0]['status'], 'ok')

    def test_stub_refuses_other_methods(self):
        # Positive control for the stub: a turn request is not silently answered.
        stub = StubCodex(self.root)
        import sessions
        with self.assertRaises(ValueError):
            with sessions.CodexClient(str(stub.path), mode='saved', timeout=5) as client:
                client.call('turn/start', {})


class BackoffTests(Base):
    def test_interval_floor(self):
        with self.assertRaisesRegex(ValueError, 'at least 5'):
            usage.Collector(self.store, 4)
        self.assertEqual(usage.Collector(self.store, 5).delay(), 300)

    def test_failures_back_off_and_success_resets(self):
        outcome = {'fail': True}
        def reader():
            if outcome['fail']:
                raise OSError('down')
            return response()
        clock = iter(T0 + timedelta(hours=h) for h in range(20))
        collector = usage.Collector(self.store, 15, reader=reader, clock=lambda: next(clock))
        delays = []
        for _ in range(6):
            collector.cycle(); delays.append(collector.delay())
        self.assertEqual(delays, [1800, 3600, 7200, 14400, 14400, 14400])
        outcome['fail'] = False
        collector.cycle()
        self.assertEqual(collector.delay(), 900)

    def test_store_failure_does_not_kill_the_thread(self):
        collector = usage.Collector(self.store, 5, reader=response, clock=lambda: T0)
        with patch.object(usage, 'sample', side_effect=RuntimeError('boom')), \
                redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()) as err:
            collector.cycle()
        self.assertEqual(collector.failures, 1)
        self.assertNotIn('boom', err.getvalue())


class QueryTests(Base):
    def test_rate_within_one_period(self):
        for minutes, used in ((0, 40), (60, 42), (120, 44)):
            self.add(T0 + timedelta(minutes=minutes), used=used)
        row = self.summary(T0 + timedelta(minutes=125))['buckets'][0]
        self.assertAlmostEqual(row['rate']['percent_per_hour'], 2.0)
        self.assertEqual(row['remaining_percent'], 56)
        self.assertEqual(row['resets'], [])

    def test_reset_boundary_splits_the_rate(self):
        for minutes, used in ((0, 80), (60, 90), (120, 5), (180, 6), (240, 8)):
            self.add(T0 + timedelta(minutes=minutes), used=used)
        now = T0 + timedelta(minutes=241)
        row = self.summary(now)['buckets'][0]
        self.assertEqual(len(row['resets']), 1)
        self.assertEqual((row['resets'][0]['used_before'], row['resets'][0]['used_after']), (90, 5))
        self.assertAlmostEqual(row['rate']['percent_per_hour'], 1.5)
        self.assertEqual(row['rate']['samples'], 3)
        self.assertIn('RESET between', usage.format_summary(self.summary(now), now))

    def test_moved_reset_time_is_a_boundary_even_when_usage_rises(self):
        self.add(T0, used=10)
        self.add(T0 + timedelta(hours=1), used=12, resets=RESET - 86400)
        row = self.summary(T0 + timedelta(hours=1))['buckets'][0]
        self.assertEqual(len(row['resets']), 1)
        self.assertIsNone(row['rate'])
        text = usage.format_summary(self.summary(T0 + timedelta(hours=1)), T0 + timedelta(hours=1))
        self.assertIn('next reset moved', text)

    def test_exhausted(self):
        self.add(T0, used=100, allowed=False, credits=0)
        text = usage.format_summary(self.summary(T0), T0)
        self.assertIn('EXHAUSTED 0% left', text)
        self.assertIn('usage not allowed', text)
        self.assertEqual(self.summary(T0)['buckets'][0]['remaining_percent'], 0)

    def test_too_few_samples_has_no_rate(self):
        self.add(T0)
        row = self.summary(T0)['buckets'][0]
        self.assertIsNone(row['rate'])
        self.assertIn('burn unknown', usage.format_summary(self.summary(T0), T0))
        # A sample outside --since does not count either.
        self.add(T0 + timedelta(hours=7), used=50)
        self.assertIsNone(self.summary(T0 + timedelta(hours=7))['buckets'][0]['rate'])

    def test_freshness_ages_out(self):
        self.add(T0)
        self.assertEqual(self.summary(T0 + timedelta(minutes=89))['buckets'][0]['freshness'], 'fresh')
        self.assertEqual(self.summary(T0 + timedelta(minutes=91))['buckets'][0]['freshness'], 'stale')

    def test_local_time_in_output(self):
        self.add(T0)
        previous = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'Asia/Seoul'; time.tzset()
            text = usage.format_summary(self.summary(T0), T0)
            self.assertIn('observed 10-23 12:00 KST', text)
            os.environ['TZ'] = 'UTC'; time.tzset()
            self.assertIn('observed 10-23 03:00 UTC', usage.format_summary(self.summary(T0), T0))
        finally:
            if previous is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = previous
            time.tzset()

    def test_since_parsing(self):
        self.assertEqual(usage.parse_since('90m'), timedelta(minutes=90))
        for bad in ('', '0h', '6', 'h6', '-1h'):
            with self.assertRaises(ValueError):
                usage.parse_since(bad)


class BrokerTests(unittest.TestCase):
    def test_broker_collects_into_its_state_dir_and_cli_reads_it(self):
        with tempfile.TemporaryDirectory(prefix='ab-stub-', dir='/tmp') as temp:
            stub = StubCodex(temp)
            with RunningBroker(usage_interval=5, codex_executable=str(stub.path)) as server:
                history = Path(server.state_dir) / 'usage' / 'codex.jsonl'
                deadline = time.monotonic() + 10
                while not history.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(history.exists())
                out = io.StringIO()
                with redirect_stdout(out):
                    code = bridge.main(['--socket', server.path, 'usage', '--state-dir', server.state_dir])
                self.assertEqual(code, 0)
                self.assertIn('codex/codex 7d: 60% left', out.getvalue())
                self.assertEqual(len(out.getvalue().splitlines()), 1)
                out = io.StringIO()
                with redirect_stdout(out):
                    bridge.main(['--socket', server.path, 'usage', '--state-dir', server.state_dir, '--json'])
                self.assertEqual(json.loads(out.getvalue())['buckets'][0]['used_percent'], 40)
            self.assertEqual(stub.methods(), list(usage.READ_METHODS))

    def test_interval_below_floor_is_refused(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = bridge.main(['--socket', '/tmp/ab-never.sock', 'serve', '--usage-interval', '4'])
        self.assertEqual(code, 1)
        self.assertIn('at least 5 minutes', out.getvalue())
        self.assertFalse(Path('/tmp/ab-never.sock').exists())


if __name__ == '__main__':
    unittest.main()
