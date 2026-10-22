"""Subscription-limit sampling (issue #7, Codex only). Plain RPC, no model turn.

The collector reads `account/rateLimits/read` from a short-lived
`codex app-server --stdio`, then stops it. It never starts a thread or a turn,
never sends to a session, and never stores account identifiers. A failed read
is recorded as unknown (null values), never as 0%.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading

import sessions

DEFAULT_INTERVAL_MINUTES = 15
MIN_INTERVAL_MINUTES = 5
RETENTION_DAYS = 30
MAX_BYTES = 5 * 1024 * 1024
DEDUP_SECONDS = 3600
MAX_BACKOFF_SECONDS = 4 * 3600
# Unchanged values are re-recorded at least hourly, so an observation older
# than the de-duplication hour plus two default intervals is stale.
STALE_AFTER_SECONDS = DEDUP_SECONDS + 2 * DEFAULT_INTERVAL_MINUTES * 60
# A resets_at that moves by more than this is a new reset period.
RESET_TOLERANCE_SECONDS = 60
SOURCE = 'codex app-server account/rateLimits/read'
FIELDS = ('observed_at', 'provider', 'limit_id', 'slot', 'used_percent', 'window_mins',
          'resets_at', 'allowed', 'reset_credits', 'source', 'status', 'reason')
# The only app-server methods a collection cycle may send.
READ_METHODS = ('initialize', 'initialized', 'account/rateLimits/read')


def utc(moment):
    return moment.astimezone(timezone.utc).isoformat()


def parse_time(value):
    return datetime.fromisoformat(value)


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalize(result, observed):
    """Map one rateLimits response to records. Keeps original units; drops identities."""
    if not isinstance(result, dict):
        raise ValueError('rate limit response is not an object')
    buckets = result.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        single = result.get('rateLimits')
        if not isinstance(single, dict) or not isinstance(single.get('limitId'), str):
            raise ValueError('rate limit response has no buckets')
        buckets = {single['limitId']: single}
    allowed = result.get('ordinaryUsageAllowed')
    allowed = allowed if isinstance(allowed, bool) else None
    credits = result.get('rateLimitResetCredits')
    credits = number(credits.get('availableCount')) if isinstance(credits, dict) else None
    records = []
    for limit_id in sorted(buckets):
        bucket = buckets[limit_id]
        if not isinstance(bucket, dict) or not isinstance(limit_id, str):
            raise ValueError('rate limit bucket is not an object')
        for slot in ('primary', 'secondary'):
            window = bucket.get(slot)
            if window is None:
                continue
            if not isinstance(window, dict):
                raise ValueError('rate limit window is not an object')
            resets = number(window.get('resetsAt'))
            resets_at = utc(datetime.fromtimestamp(resets, timezone.utc)) if resets is not None else None
            status = 'stale' if resets_at is not None and parse_time(resets_at) <= observed else 'ok'
            records.append(dict(observed_at=utc(observed), provider='codex', limit_id=limit_id,
                                slot=slot, used_percent=number(window.get('usedPercent')),
                                window_mins=number(window.get('windowDurationMins')),
                                resets_at=resets_at, allowed=allowed, reset_credits=credits,
                                source=SOURCE, status=status, reason=None))
    if not records:
        raise ValueError('rate limit response has no windows')
    return records


def failed(observed, reason):
    return dict(observed_at=utc(observed), provider='codex', limit_id=None, slot=None,
                used_percent=None, window_mins=None, resets_at=None, allowed=None,
                reset_credits=None, source=SOURCE, status='failed', reason=reason)


def read_codex(executable='codex', timeout=10):
    """One read-only RPC on a private app-server; the process is stopped on exit."""
    with sessions.CodexClient(executable, mode='saved', timeout=timeout) as client:
        return client.call('account/rateLimits/read', {})


class UsageStore:
    """Bounded, owner-private JSONL history; every write replaces the file atomically."""
    def __init__(self, directory, retention_days=RETENTION_DAYS, max_bytes=MAX_BYTES):
        self.directory = Path(directory).expanduser().absolute()
        self.path = self.directory / 'codex.jsonl'
        self.retention = timedelta(days=retention_days)
        self.max_bytes = max_bytes

    def private(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError('Usage state directory must be owner-private (0700)')

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, 'r', encoding='utf-8') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Usage history must be an owner-private regular file')
            records = []
            for line in source:
                try:
                    item = json.loads(line)
                    parse_time(item['observed_at'])
                    records.append({field: item.get(field) for field in FIELDS})
                except (ValueError, KeyError, TypeError):
                    # Dropped on the next write. Never echo the line itself.
                    print(json.dumps(dict(usage_drop='codex', reason='invalid history line')),
                          file=sys.stderr, flush=True)
            return records

    def bound(self, records, now):
        cutoff = now - self.retention
        records = [item for item in records if parse_time(item['observed_at']) >= cutoff]
        lines = [json.dumps(item, ensure_ascii=False) + '\n' for item in records]
        size = sum(len(line.encode()) for line in lines)
        start = 0
        while start < len(lines) and size > self.max_bytes:
            size -= len(lines[start].encode())
            start += 1
        return records[start:], ''.join(lines[start:]).encode()

    def save(self, records, now):
        self.private()
        records, data = self.bound(records, now)
        name = None
        try:
            fd, name = tempfile.mkstemp(prefix='.codex-', dir=self.directory)
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            name = None
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)
        return records

    def append(self, new, now):
        """Add samples, skipping any equal to its bucket's last one within the hour."""
        records = self.load()
        last = {}
        for item in records:
            last[(item['limit_id'], item['slot'])] = item
        kept = []
        for item in new:
            previous = last.get((item['limit_id'], item['slot']))
            same = previous is not None and all(previous[field] == item[field] for field in (
                'used_percent', 'window_mins', 'resets_at', 'allowed', 'reset_credits', 'status'))
            if same and (now - parse_time(previous['observed_at'])).total_seconds() < DEDUP_SECONDS:
                continue
            kept.append(item)
        if kept or len(self.bound(records, now)[0]) != len(records):
            self.save(records + kept, now)
        return kept


def failure_reason(error):
    # Exception text can carry app-server stderr or account payloads: keep a category only.
    if 'timed out' in str(error):
        return 'timeout'
    if isinstance(error, FileNotFoundError):
        return 'codex executable not found'
    if isinstance(error, OSError):
        return 'app-server unavailable'
    return 'invalid or failed response'


def sample(store, reader, clock):
    now = clock()
    try:
        records = normalize(reader(), now)
    except (ValueError, OSError, KeyError, TypeError, OverflowError,
            subprocess.TimeoutExpired) as error:
        records = [failed(now, failure_reason(error))]
    store.append(records, now)
    return records


class Collector:
    """Broker-resident daemon thread. Owns no session, route or message state."""
    def __init__(self, store, interval_minutes=DEFAULT_INTERVAL_MINUTES, executable='codex',
                 reader=None, clock=None):
        if interval_minutes < MIN_INTERVAL_MINUTES:
            raise ValueError(f'Usage interval must be at least {MIN_INTERVAL_MINUTES} minutes')
        self.store, self.interval = store, interval_minutes * 60
        self.reader = reader or (lambda: read_codex(executable))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.failures = 0
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name='usage-collector', daemon=True)

    def delay(self):
        if not self.failures:
            return self.interval
        return min(self.interval * 2 ** min(self.failures, 8), max(self.interval, MAX_BACKOFF_SECONDS))

    def cycle(self):
        try:
            records = sample(self.store, self.reader, self.clock)
        except Exception as error:  # noqa: BLE001 - the thread must outlive any one cycle.
            # Storage failures never stop the broker; report without paths or payloads.
            print(json.dumps(dict(usage_sample='codex', status='store_failed',
                                  reason=type(error).__name__)), file=sys.stderr, flush=True)
            self.failures += 1
            return
        self.failures = self.failures + 1 if records[0]['status'] == 'failed' else 0

    def run(self):
        while not self.stopping.is_set():
            self.cycle()
            self.stopping.wait(self.delay())

    def start(self):
        self.thread.start()
        return self

    def stop(self, timeout=1):
        # Never hold broker shutdown for a read in flight; the thread is a daemon.
        self.stopping.set()
        if self.thread.is_alive():
            self.thread.join(timeout)


# --- query -------------------------------------------------------------------

def parse_since(value):
    match = re.fullmatch(r'([1-9][0-9]*)([mhd])', value or '')
    if not match:
        raise ValueError('--since takes a duration such as 90m, 6h or 2d')
    return timedelta(**{dict(m='minutes', h='hours', d='days')[match[2]]: int(match[1])})


def is_reset(before, after):
    if after['used_percent'] < before['used_percent']:
        return True
    if before['resets_at'] is None or after['resets_at'] is None:
        return before['resets_at'] != after['resets_at']
    moved = abs((parse_time(after['resets_at']) - parse_time(before['resets_at'])).total_seconds())
    return moved > RESET_TOLERANCE_SECONDS


def summarize(records, now, since):
    """Per bucket: latest values, freshness, resets and the burn rate inside one period."""
    cutoff = now - since
    latest_failure = max((parse_time(r['observed_at']) for r in records if r['status'] == 'failed'),
                         default=None)
    buckets = {}
    for item in records:
        if item['status'] != 'failed':
            buckets.setdefault((item['limit_id'], item['slot']), []).append(item)
    rows = []
    for (limit_id, slot), items in sorted(buckets.items()):
        items.sort(key=lambda r: r['observed_at'])
        last = items[-1]
        observed = parse_time(last['observed_at'])
        age = (now - observed).total_seconds()
        failed_after = latest_failure is not None and latest_failure > observed
        freshness = ('failed' if failed_after else 'stale' if age > STALE_AFTER_SECONDS
                     or last['status'] == 'stale' else 'fresh')
        window = [r for r in items if parse_time(r['observed_at']) >= cutoff
                  and r['used_percent'] is not None]
        resets, segment = [], window[:1]
        for before, after in zip(window, window[1:]):
            if is_reset(before, after):
                resets.append(dict(after=before['observed_at'], before=after['observed_at'],
                                   used_before=before['used_percent'], used_after=after['used_percent'],
                                   resets_at_before=before['resets_at'], resets_at_after=after['resets_at']))
                segment = [after]
            else:
                segment.append(after)
        rate = None
        if len(segment) >= 2:
            hours = (parse_time(segment[-1]['observed_at'])
                     - parse_time(segment[0]['observed_at'])).total_seconds() / 3600
            if hours > 0:
                rate = dict(percent_per_hour=(segment[-1]['used_percent'] - segment[0]['used_percent']) / hours,
                            hours=hours, samples=len(segment))
        used = last['used_percent']
        rows.append(dict(provider=last['provider'], limit_id=limit_id, slot=slot,
                         used_percent=used, remaining_percent=None if used is None else max(0, 100 - used),
                         window_mins=last['window_mins'], resets_at=last['resets_at'],
                         allowed=last['allowed'], reset_credits=last['reset_credits'],
                         observed_at=last['observed_at'], age_seconds=age, freshness=freshness,
                         rate=rate, resets=resets))
    return dict(buckets=rows, last_failure=None if latest_failure is None else utc(latest_failure))


def local(value):
    return parse_time(value).astimezone().strftime('%m-%d %H:%M %Z')


def span(seconds):
    seconds = int(abs(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f'{days}d{hours}h' if days else f'{hours}h{minutes:02d}m' if hours else f'{minutes}m'


def window_name(minutes):
    if minutes is None:
        return 'window ?'
    return f'{minutes // 1440}d' if minutes % 1440 == 0 else f'{minutes // 60}h' if minutes % 60 == 0 else f'{minutes}m'


def format_summary(summary, now):
    lines = []
    for row in summary['buckets']:
        name = f"{row['provider']}/{row['limit_id']}" + ('' if row['slot'] == 'primary' else f"/{row['slot']}")
        remaining = 'unknown' if row['remaining_percent'] is None else f"{row['remaining_percent']:g}% left"
        if row['used_percent'] is not None and row['used_percent'] >= 100:
            remaining = 'EXHAUSTED 0% left'
        if row['resets_at'] is None:
            resets = 'resets unknown'
        else:
            until = (parse_time(row['resets_at']) - now).total_seconds()
            resets = (f"resets {local(row['resets_at'])} (in {span(until)})" if until > 0
                      else f"reset was due {local(row['resets_at'])}")
        parts = [f"{name} {window_name(row['window_mins'])}: {remaining}", resets,
                 f"observed {local(row['observed_at'])} ({span(row['age_seconds'])} ago, {row['freshness']})"]
        if row['allowed'] is False:
            parts.append('usage not allowed')
        if row['rate']:
            parts.append(f"burn {row['rate']['percent_per_hour']:+.2f}%p/h over {span(row['rate']['hours'] * 3600)}")
        else:
            parts.append('burn unknown (fewer than 2 samples in this period)')
        lines.append(' · '.join(parts))
        for boundary in row['resets']:
            lines.append(f"  RESET between {local(boundary['after'])} and {local(boundary['before'])}: "
                         f"used {boundary['used_before']:g}% -> {boundary['used_after']:g}%"
                         + ('' if boundary['resets_at_before'] == boundary['resets_at_after']
                            else ', next reset moved'))
    if not summary['buckets']:
        lines.append('codex: unknown (no successful read recorded)')
    if summary['last_failure']:
        lines.append(f"codex: last failed read {local(summary['last_failure'])}")
    return '\n'.join(lines)


def query(state_dir, since='6h', provider='codex', now=None):
    if provider != 'codex':
        raise ValueError('Only the codex provider is collected')
    now = now or datetime.now(timezone.utc)
    store = UsageStore(Path(state_dir).expanduser() / 'usage')
    records = store.load()
    return records, summarize(records, now, parse_since(since)), now
