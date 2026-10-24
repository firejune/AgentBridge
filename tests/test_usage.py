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

    def test_resets_at_drift_within_tolerance_is_deduplicated(self):
        self.add(T0)
        self.add(T0 + timedelta(minutes=15), resets=RESET + 1)  # measured live: 1 s drift
        self.add(T0 + timedelta(minutes=30), resets=RESET - 1)
        self.assertEqual(len(self.store.load()), 1)
        self.add(T0 + timedelta(minutes=45), resets=RESET + 2 * 3600)  # a real move
        self.assertEqual([r['resets_at'] for r in self.store.load()],
                         [usage.utc(datetime.fromtimestamp(t, timezone.utc)) for t in (RESET, RESET + 7200)])
        self.add(T0 + timedelta(minutes=50), resets=None)
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



KST = timezone(timedelta(hours=9))
# The six promo-reset announcement posts (publication times, KST).
ANNOUNCED = ['2026-10-07 12:35', '2026-10-11 07:16', '2026-10-13 10:03',
             '2026-10-15 13:55', '2026-10-18 01:20', '2026-10-20 15:33']


def kst(text):
    return datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=KST)


def ts(moment):
    return int(moment.timestamp())


class EventBase(Base):
    def setUp(self):
        super().setUp()
        self.events = usage.EventStore(self.store.directory)
        self.announcements = usage.AnnouncementStore(self.store.directory)

    def add(self, moment, used=40, resets=RESET, **fields):
        return usage.sample(self.store, lambda: response(used, resets, **fields), lambda: moment, self.events)

    def seed(self, times=ANNOUNCED):
        path = self.root / 'announcements.jsonl'
        path.write_text(''.join(json.dumps(dict(at=kst(t).isoformat(), source='announcement', note='post')) + '\n'
                                for t in times))
        return self.announcements.seed(path, T0)

    def query(self, now):
        return usage.query(self.root / 'state', '6h', now=now)[1]

    def early_resets(self, start, count, every_hours, resets_after_days=7):
        """`count` early resets, each 15 minutes wide, with the due time a week out."""
        moment, due = start, start + timedelta(days=resets_after_days)
        self.add(moment, used=80, resets=ts(due))
        for _ in range(count):
            moment += timedelta(hours=every_hours)
            self.add(moment - timedelta(minutes=15), used=90, resets=ts(due))
            due = moment + timedelta(days=resets_after_days)
            self.add(moment, used=0, resets=ts(due))
        return moment


class ResetEventTests(EventBase):
    def test_scheduled_when_the_window_contains_the_due_time(self):
        due = T0 + timedelta(hours=1)
        self.add(T0 + timedelta(minutes=50), used=70, resets=ts(due))
        self.add(T0 + timedelta(minutes=65), used=0, resets=ts(due + timedelta(days=7)))
        [event] = self.events.load()
        self.assertEqual(event['kind'], 'scheduled')
        self.assertEqual(event['source'], 'observed')
        self.assertEqual((event['window_start'], event['window_end']),
                         (usage.utc(T0 + timedelta(minutes=50)), usage.utc(T0 + timedelta(minutes=65))))
        self.assertEqual((event['used_before'], event['used_after']), (70, 0))
        self.assertEqual((event['resets_at_before'], event['resets_at_after']),
                         (usage.utc(due), usage.utc(due + timedelta(days=7))))

    def test_late_reset_and_one_inside_the_tolerance_are_scheduled(self):
        due = T0 + timedelta(hours=1)
        self.add(T0 + timedelta(minutes=70), used=70, resets=ts(due))  # stale: due passed
        self.add(T0 + timedelta(minutes=85), used=0, resets=ts(due + timedelta(days=7)))
        self.assertEqual(self.events.load()[0]['kind'], 'scheduled')
        due2 = T0 + timedelta(days=7, hours=1)
        self.add(due2 - timedelta(minutes=30), used=5, resets=ts(due + timedelta(days=7)))
        self.add(due2 - timedelta(minutes=10), used=0, resets=ts(due2 + timedelta(days=7)))
        self.assertEqual([e['kind'] for e in self.events.load()], ['scheduled', 'scheduled'])

    def test_early_when_the_window_closes_before_the_due_time(self):
        due = T0 + timedelta(days=4)
        self.add(T0, used=100, resets=ts(due))
        self.add(T0 + timedelta(minutes=15), used=0, resets=ts(due + timedelta(days=1)))
        [event] = self.events.load()
        self.assertEqual(event['kind'], 'early')
        # A promo reset that leaves used at 0 is still seen through the moved resets_at.
        self.add(T0 + timedelta(hours=60), used=0, resets=ts(due + timedelta(days=3)))
        self.assertEqual([e['kind'] for e in self.events.load()], ['early', 'early'])

    def test_failed_reads_between_widen_the_window_to_the_ok_samples(self):
        due = T0 + timedelta(days=4)
        self.add(T0, used=60, resets=ts(due))
        for minutes in (15, 30, 45):
            usage.sample(self.store, lambda: (_ for _ in ()).throw(OSError('down')),
                         lambda m=minutes: T0 + timedelta(minutes=m), self.events)
        self.add(T0 + timedelta(minutes=60), used=1, resets=ts(due + timedelta(days=2)))
        [event] = self.events.load()
        self.assertEqual((event['window_start'], event['window_end'], event['kind']),
                         (usage.utc(T0), usage.utc(T0 + timedelta(hours=1)), 'early'))

    def test_unknown_due_time_is_not_classified(self):
        self.add(T0, used=60, resets=None)
        self.add(T0 + timedelta(minutes=15), used=0, resets=RESET)
        self.assertEqual(self.events.load()[0]['kind'], 'unknown')

    def test_events_are_deduplicated_and_survive_sample_retention(self):
        self.add(T0 - timedelta(days=29), used=100, resets=ts(T0))
        self.add(T0 - timedelta(days=29) + timedelta(minutes=15), used=0, resets=ts(T0 + timedelta(days=2)))
        for hour in range(1, 4):  # every later cycle re-derives the same event
            self.add(T0 - timedelta(days=29) + timedelta(hours=hour), used=hour, resets=ts(T0 + timedelta(days=2)))
        self.assertEqual(len(self.events.load()), 1)
        self.assertEqual(len(self.events.merge(usage.detect(self.store.load()), T0)), 0)
        self.add(T0 + timedelta(days=2), used=9, resets=ts(T0 + timedelta(days=2)))
        self.assertTrue(all(usage.parse_time(r['observed_at']) >= T0 for r in self.store.load()))
        self.assertEqual(len(self.events.load()), 1)
        self.assertEqual(len(self.query(T0 + timedelta(days=2))['buckets'][0]['events']), 1)

    def test_query_derives_events_not_yet_persisted(self):
        usage.sample(self.store, lambda: response(90, ts(T0 + timedelta(days=3))), lambda: T0)
        usage.sample(self.store, lambda: response(0, ts(T0 + timedelta(days=9))), lambda: T0 + timedelta(minutes=15))
        self.assertFalse(self.events.path.exists())
        self.assertEqual([e['kind'] for e in self.query(T0 + timedelta(minutes=16))['buckets'][0]['events']],
                         ['early'])
        self.assertFalse(self.events.path.exists())  # a query never writes

    def test_the_broker_collector_persists_events(self):
        values = iter([response(100, ts(T0 + timedelta(days=4))), response(0, ts(T0 + timedelta(days=9)))])
        clock = iter([T0, T0 + timedelta(minutes=15)])
        collector = usage.Collector(self.store, 15, reader=lambda: next(values), clock=lambda: next(clock))
        collector.cycle(); collector.cycle()
        self.assertEqual([e['kind'] for e in self.events.load()], ['early'])

    def test_events_file_is_private_and_atomic(self):
        self.early_resets(T0, 1, 60)
        self.assertEqual(stat.S_IMODE(self.events.path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.store.directory.iterdir()),
                         ['codex-events.jsonl', 'codex.jsonl'])


class EstimateTests(EventBase):
    def test_six_announcements_give_the_published_statistics(self):
        self.assertEqual(len(self.seed()), 6)
        now = kst('2026-10-21 00:00')
        self.add(now)
        guess = self.query(now)['buckets'][0]['early_reset']
        self.assertEqual((guess['source'], guess['n']), ('announcement', 5))
        self.assertAlmostEqual(guess['mean_hours'], 62.9933, places=3)
        self.assertAlmostEqual(guess['median_hours'], 59.4167, places=3)
        self.assertAlmostEqual(guess['min_hours'], 50.7833, places=3)
        self.assertAlmostEqual(guess['max_hours'], 90.6833, places=3)
        self.assertEqual(usage.parse_time(guess['centre']), kst('2026-10-23 02:58'))
        self.assertEqual(usage.parse_time(guess['low']), kst('2026-10-22 18:20'))
        self.assertEqual(usage.parse_time(guess['high']), kst('2026-10-24 10:14'))
        self.assertEqual(guess['state'], 'ahead')

    def test_observed_intervals_win_once_there_are_three(self):
        self.seed()
        last = self.early_resets(T0, 4, 48)
        guess = self.query(last + timedelta(hours=1))['buckets'][0]['early_reset']
        self.assertEqual((guess['source'], guess['n'], guess['counts']['announcement']), ('observed', 3, 5))
        self.assertAlmostEqual(guess['median_hours'], 48)
        self.assertEqual(usage.parse_time(guess['centre']), last - timedelta(minutes=7.5) + timedelta(hours=48))

    def test_fewer_observed_intervals_fall_back_to_announcements_unpooled(self):
        self.seed()
        last = self.early_resets(kst('2026-10-21 00:00'), 3, 30)
        guess = self.query(last + timedelta(hours=1))['buckets'][0]['early_reset']
        self.assertEqual(guess['counts'], dict(observed=2, announcement=5))
        self.assertEqual((guess['source'], guess['n']), ('announcement', 5))
        self.assertAlmostEqual(guess['median_hours'], 59.4167, places=3)
        self.assertAlmostEqual(guess['min_hours'], 50.7833, places=3)
        # The anchor is the latest early reset of either source: here the observed one.
        self.assertEqual(guess['anchor_source'], 'observed')
        self.assertEqual(usage.parse_time(guess['anchor']), last - timedelta(minutes=7.5))

    def test_too_few_prints_no_time_and_spends_to_the_official_reset(self):
        self.seed(ANNOUNCED[:3])
        last = self.early_resets(T0, 2, 50)
        now = last + timedelta(hours=1)
        row = self.query(now)['buckets'][0]
        self.assertEqual(row['early_reset'], dict(state='too_few', counts=dict(observed=1, announcement=2)))
        self.assertEqual((row['spend_horizon'], row['spend_basis']), (row['resets_at'], 'official'))
        self.assertEqual(row['stop_risk_horizon'], row['resets_at'])
        text = usage.format_events(self.query(now), self.query(now)['announcements'])
        self.assertIn('too few', text)
        self.assertNotIn('next early reset', text)
        self.assertNotIn('next early reset', usage.format_summary(self.query(now), now))

    def test_spend_horizon_is_the_estimate_when_it_comes_first(self):
        self.seed()
        now = kst('2026-10-21 00:00')
        self.add(now, used=70, resets=ts(kst('2026-10-27 15:46')))
        row = self.query(now)['buckets'][0]
        self.assertEqual(usage.parse_time(row['spend_horizon']), kst('2026-10-23 02:58'))
        self.assertEqual(row['spend_basis'], 'estimate')
        self.assertEqual(usage.parse_time(row['stop_risk_horizon']), kst('2026-10-27 15:46'))

    def test_spend_horizon_is_official_when_it_comes_first(self):
        self.seed()
        now = kst('2026-10-21 00:00')
        self.add(now, used=70, resets=ts(kst('2026-10-22 09:00')))
        row = self.query(now)['buckets'][0]
        self.assertEqual(row['early_reset']['state'], 'ahead')
        self.assertEqual((usage.parse_time(row['spend_horizon']), row['spend_basis']),
                         (kst('2026-10-22 09:00'), 'official'))
        self.assertEqual(usage.parse_time(row['stop_risk_horizon']), kst('2026-10-22 09:00'))

    def test_overdue_is_kept_and_a_lapsed_estimate_is_dropped(self):
        self.seed()
        official = ts(kst('2026-10-27 15:46'))
        self.add(kst('2026-10-23 05:00'), used=70, resets=official)
        row = self.query(kst('2026-10-23 05:00'))['buckets'][0]
        self.assertEqual(row['early_reset']['state'], 'overdue')
        self.assertEqual(row['spend_basis'], 'estimate')
        self.add(kst('2026-10-24 11:00'), used=75, resets=official)
        row = self.query(kst('2026-10-24 11:00'))['buckets'][0]
        self.assertEqual(row['early_reset']['state'], 'lapsed')
        self.assertEqual((row['spend_basis'], row['spend_horizon']), ('official', row['resets_at']))
        self.assertNotIn('next early reset', usage.format_summary(self.query(kst('2026-10-24 11:00')),
                                                                 kst('2026-10-24 11:00')))

    def test_a_wide_window_breaks_the_interval_chain_and_cannot_anchor(self):
        last = self.early_resets(T0, 4, 48)
        self.assertEqual(self.query(last)['buckets'][0]['early_reset']['n'], 3)
        due = last + timedelta(days=7)
        # Broker down for four hours across the next early reset.
        self.add(last + timedelta(hours=44), used=50, resets=ts(due))
        self.add(last + timedelta(hours=48), used=0, resets=ts(due + timedelta(days=2)))
        row = self.query(last + timedelta(hours=49))['buckets'][0]
        wide = row['events'][-1]
        self.assertEqual(usage.width(wide), 4 * 3600)
        self.assertGreater(usage.width(wide), usage.MAX_EVENT_WINDOW_SECONDS)
        self.assertEqual(wide['kind'], 'early')  # still listed and classified
        self.assertEqual(row['early_reset']['n'], 3)  # but no interval to it
        self.assertEqual(row['early_reset']['state'], 'no_anchor')
        self.assertEqual(row['spend_basis'], 'official')
        text = usage.format_events(self.query(last + timedelta(hours=49)), [])
        self.assertIn('too wide', text)

    def test_announcements_need_an_offset_and_are_stored_in_utc(self):
        path = self.root / 'bad.jsonl'
        path.write_text('{"at": "2026-10-07T12:35:00", "note": "x"}\n')
        with self.assertRaisesRegex(ValueError, 'no UTC offset'):
            self.announcements.seed(path, T0)
        path.write_text('{"at": "2026-10-07T12:35:00+09:00", "source": "observed"}\n')
        with self.assertRaisesRegex(ValueError, 'source must be'):
            self.announcements.seed(path, T0)
        self.assertFalse(self.announcements.path.exists())
        self.seed()
        self.assertEqual(self.announcements.load()[0]['at'], '2026-10-07T03:35:00+00:00')
        self.assertEqual(self.seed(), [])  # idempotent
        self.assertEqual(len(self.announcements.load()), 6)

    def test_local_time_in_event_output(self):
        self.seed()
        self.add(kst('2026-10-21 00:00'), used=70, resets=ts(kst('2026-10-27 15:46')))
        summary = self.query(kst('2026-10-21 00:00'))
        previous = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'Asia/Seoul'; time.tzset()
            text = usage.format_events(summary, summary['announcements'])
            self.assertIn('spend horizon: 10-23 02:58 KST (estimate)', text)
            self.assertIn('stop risk horizon: 10-27 15:46 KST (official resets_at)', text)
            self.assertIn('mean 63.0h · median 59.4h · range 50.8h–90.7h', text)
            self.assertIn('[est, n=5, announcement]', usage.format_summary(summary, kst('2026-10-21 00:00')))
            os.environ['TZ'] = 'UTC'; time.tzset()
            text = usage.format_events(summary, summary['announcements'])
            self.assertIn('spend horizon: 10-22 17:58 UTC (estimate)', text)
            self.assertIn('window 10-22 09:20 UTC … 10-24 01:14 UTC', text)
        finally:
            if previous is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = previous
            time.tzset()


class EventCliTests(EventBase):
    def test_events_and_seeding_never_collect_or_call(self):
        self.add(T0, used=90, resets=ts(T0 + timedelta(days=3)))
        self.add(T0 + timedelta(minutes=15), used=0, resets=ts(T0 + timedelta(days=9)))
        path = self.root / 'announcements.jsonl'
        path.write_text(''.join(json.dumps(dict(at=kst(t).isoformat(), note='post')) + '\n' for t in ANNOUNCED))
        forbidden = AssertionError('a query must not collect or call anything')
        state = str(self.root / 'state')
        import sessions
        with patch.object(usage, 'read_codex', side_effect=forbidden), \
                patch.object(usage, 'sample', side_effect=forbidden), \
                patch.object(sessions, 'CodexClient', side_effect=forbidden), \
                patch('subprocess.Popen', side_effect=forbidden), \
                patch('socket.socket', side_effect=forbidden):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(bridge.main(['--socket', '/tmp/ab-never.sock', 'usage', '--state-dir', state,
                                              '--seed-announcements', str(path)]), 0)
                self.assertEqual(bridge.main(['--socket', '/tmp/ab-never.sock', 'usage', '--state-dir', state,
                                              '--events']), 0)
                self.assertEqual(bridge.main(['--socket', '/tmp/ab-never.sock', 'usage', '--state-dir', state,
                                              '--events', '--json']), 0)
                self.assertEqual(bridge.main(['--socket', '/tmp/ab-never.sock', 'usage', '--state-dir', state]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0])['added'], 6)
        text = out.getvalue()
        self.assertIn('announcements (6):', text)
        self.assertIn('early-reset intervals [announcement, n=5]', text)
        self.assertIn('spend horizon:', text)
        self.assertIn('stop risk horizon:', text)
        self.assertIn('[observed]', text)
        data = json.loads(next(line for line in lines if line.startswith('{"announcements"')))
        self.assertEqual(data['buckets'][0]['early_reset']['source'], 'announcement')
        self.assertIn('next early reset ~', [line for line in lines if line.startswith('codex/codex 7d:')][-1])
        self.assertEqual(len(self.store.load()), 2)  # nothing was sampled

if __name__ == '__main__':
    unittest.main()


class RiskTests(Base):
    def brief(self, now):
        return usage.brief(self.root / 'state', now=now)

    def hourly(self, *used, **fields):
        for hour, value in enumerate(used):
            self.add(T0 + timedelta(minutes=61 * hour), used=value, **fields)
        return T0 + timedelta(minutes=61 * (len(used) - 1) + 1)

    def test_burn_that_empties_before_reset_is_at_risk_with_the_empty_time(self):
        now = self.hourly(40, 42, 44)
        found = self.brief(now)
        (row,) = found['buckets']
        self.assertEqual((found['state'], row['state']), ('at_risk', 'at_risk'))
        hours = 56 / row['burn_per_hour']
        self.assertAlmostEqual(usage.parse_time(row['empty_at']).timestamp(), (now + timedelta(hours=hours)).timestamp(), delta=1)
        self.assertIn('AT RISK: empty ~', found['lines'][0])
        self.assertIn('(3h)', found['lines'][0])

    def test_slow_or_flat_burn_holds(self):
        self.assertEqual(self.brief(self.hourly(40, 40.5, 41))['state'], 'holds')
        self.tearDown(); self.setUp()
        self.assertEqual(self.brief(self.hourly(40, 40, 40))['state'], 'holds')

    def test_exhausted_beats_everything(self):
        self.add(T0, used=100, allowed=False)
        self.assertEqual(self.brief(T0)['state'], 'exhausted')

    def test_missing_stale_sparse_and_due_are_unknown(self):
        self.assertEqual(self.brief(T0)['state'], 'unknown')  # nothing recorded
        self.add(T0)
        self.assertIn('fewer than 2 samples', self.brief(T0)['buckets'][0]['reason'])
        self.assertIn('stale', self.brief(T0 + timedelta(minutes=91))['buckets'][0]['reason'])
        self.tearDown(); self.setUp()
        soon = int((T0 + timedelta(minutes=90)).timestamp())
        now = self.hourly(40, 41, resets=soon)
        self.assertEqual(self.brief(now + timedelta(minutes=30))['buckets'][0]['reason'], 'reset is due; waiting for a new sample')

    def test_broken_store_is_unknown_and_never_raises(self):
        with patch.object(usage, 'query', side_effect=OSError('disk gone')):
            found = self.brief(T0)
        self.assertEqual(found['state'], 'unknown')
        self.assertIn('disk gone', found['reason'])

    def test_usage_line_carries_the_state(self):
        now = self.hourly(40, 42, 44)
        text = usage.format_summary(usage.query(self.root / 'state', '6h', now=now)[1], now)
        self.assertIn('AT RISK', text)


class MessageUsageTests(Base):
    def setUp(self):
        super().setUp()
        for hour, value in enumerate((40, 42, 44)):
            self.add(T0 + timedelta(minutes=61 * hour), used=value)

    def usage_for(self, *transports):
        message = {'route': {'transport': transports[0]}, 'return_route': {'transport': transports[1]}}
        with patch.object(bridge, 'broker_state_dir', return_value=self.root / 'state'):
            return bridge.message_usage(None, message)

    def test_explicit_state_dir_is_where_usage_is_read(self):
        import argparse
        args = argparse.Namespace(socket=str(self.root / 'elsewhere.sock'), state_dir=str(self.root / 'state'))
        found = bridge.message_usage(args, {'route': {'transport': 'codex'}, 'return_route': {}})
        self.assertNotIn('no successful read', ' '.join(found['lines']))
        self.assertEqual(len(found['buckets']), 1)
        args.state_dir = None  # socket-derived default holds nothing here
        self.assertIn('no successful read', bridge.message_usage(args, {'route': {'transport': 'codex'}})['lines'][0])

    def test_codex_on_either_end_carries_usage_and_others_do_not(self):
        self.assertEqual(self.usage_for('codex', 'claude-peer')['provider'], 'codex')
        self.assertEqual(self.usage_for('claude-peer', 'codex')['provider'], 'codex')
        self.assertIsNone(self.usage_for('claude-peer', 'agy'))
        self.assertIsNone(self.usage_for('realtime', 'realtime'))

    def test_send_output_carries_usage_only_for_codex_routes(self):
        with RunningBroker() as server, patch.object(bridge, 'broker_state_dir', return_value=self.root / 'state'):
            cwd = str(self.root)
            server.call('register', project='p', name='hq', cwd=cwd)
            server.call('register', project='p', name='w', cwd=cwd)
            body = self.root / 'b.txt'; body.write_text('hi')
            out = io.StringIO()
            with redirect_stdout(out):
                code = bridge.main(['--socket', server.path, 'send', '--project', 'p', '--from', 'hq', '--to', 'w',
                                    '--key', 'k1', '--file', str(body)])
            self.assertEqual(code, 0)
            self.assertNotIn('usage', json.loads(out.getvalue()))
            sent = server.call('send', project='p', sender='hq', recipient='w', body='x', key='k2')
            with patch.object(bridge, 'push_if_peer', side_effect=lambda path, m: {**m, 'route': {**m['route'], 'transport': 'codex'}}):
                out = io.StringIO()
                with redirect_stdout(out):
                    bridge.main(['--socket', server.path, 'reply', sent['id'], '--key', 'k3', '--file', str(body)])
            carried = json.loads(out.getvalue())['usage']
            # Real clock: the fixture samples are stale, so the honest verdict is unknown.
            self.assertEqual((carried['provider'], carried['state']), ('codex', 'unknown'))
            self.assertIn('stale', carried['lines'][0])
