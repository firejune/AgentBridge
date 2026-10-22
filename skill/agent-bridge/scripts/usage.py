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

# Reset events outlive the samples they were derived from.
EVENT_RETENTION_DAYS = 365
EVENT_MAX_BYTES = 1024 * 1024
EVENT_FIELDS = ('provider', 'limit_id', 'slot', 'kind', 'source', 'window_start', 'window_end',
                'used_before', 'used_after', 'resets_at_before', 'resets_at_after', 'note')
ANNOUNCEMENT_FIELDS = ('at', 'source', 'note')
# A reset seen within one default sampling interval of the due resets_at is the
# scheduled one; only a window that closes earlier than that is early.
SCHEDULE_TOLERANCE_SECONDS = DEFAULT_INTERVAL_MINUTES * 60
# Wider observation windows (broker down, long backoff) are listed but neither
# measure an interval nor anchor an estimate: their timing is too loose.
MAX_EVENT_WINDOW_SECONDS = 3 * 3600
MIN_INTERVALS = 3


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
    name, fields, key = 'codex.jsonl', FIELDS, 'observed_at'

    def __init__(self, directory, retention_days=RETENTION_DAYS, max_bytes=MAX_BYTES):
        self.directory = Path(directory).expanduser().absolute()
        self.path = self.directory / self.name
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
                    parse_time(item[self.key])
                    records.append({field: item.get(field) for field in self.fields})
                except (ValueError, KeyError, TypeError):
                    # Dropped on the next write. Never echo the line itself.
                    print(json.dumps(dict(usage_drop='codex', reason='invalid history line')),
                          file=sys.stderr, flush=True)
            return records

    def bound(self, records, now):
        cutoff = now - self.retention
        records = [item for item in records if parse_time(item[self.key]) >= cutoff]
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
            fd, name = tempfile.mkstemp(prefix='.' + self.name.split('.')[0] + '-', dir=self.directory)
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
            same = previous is not None and same_reset(previous['resets_at'], item['resets_at']) and all(
                previous[field] == item[field] for field in (
                    'used_percent', 'window_mins', 'allowed', 'reset_credits', 'status'))
            if same and (now - parse_time(previous['observed_at'])).total_seconds() < DEDUP_SECONDS:
                continue
            kept.append(item)
        if kept or len(self.bound(records, now)[0]) != len(records):
            self.save(records + kept, now)
        return kept


class EventStore(UsageStore):
    """Reset events derived from the samples, kept for a year and de-duplicated by window."""
    name, fields, key = 'codex-events.jsonl', EVENT_FIELDS, 'window_end'

    def __init__(self, directory, retention_days=EVENT_RETENTION_DAYS, max_bytes=EVENT_MAX_BYTES):
        super().__init__(directory, retention_days, max_bytes)

    def merge(self, events, now):
        """Add events whose (bucket, window) is new; written only when something is added."""
        records = self.load()
        known = {event_key(item) for item in records}
        kept = [item for item in events if event_key(item) not in known]
        for item in kept:
            known.add(event_key(item))
        if kept:
            self.save(sorted(records + kept, key=lambda r: (r['window_end'], r['window_start'])), now)
        return kept


class AnnouncementStore(UsageStore):
    """External promo-reset announcements, written only by `usage --seed-announcements`."""
    name, fields, key = 'codex-announcements.jsonl', ANNOUNCEMENT_FIELDS, 'at'

    def __init__(self, directory, retention_days=EVENT_RETENTION_DAYS, max_bytes=EVENT_MAX_BYTES):
        super().__init__(directory, retention_days, max_bytes)

    def seed(self, path, now):
        """Merge announcements from a JSONL file; `at` needs an explicit UTC offset."""
        new = []
        for number_, line in enumerate(Path(path).expanduser().read_text(encoding='utf-8').splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                at = parse_time(item['at'])
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(f'announcement line {number_}: needs an ISO 8601 "at"') from error
            if at.utcoffset() is None:
                raise ValueError(f'announcement line {number_}: "at" has no UTC offset')
            if item.get('source', 'announcement') != 'announcement':
                raise ValueError(f'announcement line {number_}: source must be "announcement"')
            note = item.get('note')
            new.append(dict(at=utc(at), source='announcement', note=note if isinstance(note, str) else None))
        records = self.load()
        known = {parse_time(item['at']) for item in records}
        kept = []
        for item in new:
            if parse_time(item['at']) not in known:
                known.add(parse_time(item['at']))
                kept.append(item)
        if kept:
            self.save(sorted(records + kept, key=lambda r: r['at']), now)
        return kept


def event_key(event):
    return (event['source'], event['limit_id'], event['slot'], event['window_start'], event['window_end'])


def classify(before, after):
    """scheduled / early / unknown from the due time the earlier sample reported."""
    if before['resets_at'] is None:
        return 'unknown'
    closes = parse_time(after['observed_at'])
    due = parse_time(before['resets_at'])
    return 'early' if (due - closes).total_seconds() > SCHEDULE_TOLERANCE_SECONDS else 'scheduled'


def detect(records):
    """Reset events between consecutive successful samples of each bucket. Reads only."""
    buckets = {}
    for item in records:
        if item['status'] != 'failed' and item['used_percent'] is not None:
            buckets.setdefault((item['limit_id'], item['slot']), []).append(item)
    events = []
    for (limit_id, slot), items in sorted(buckets.items()):
        items.sort(key=lambda r: r['observed_at'])
        for before, after in zip(items, items[1:]):
            if is_reset(before, after):
                events.append(dict(provider=before['provider'], limit_id=limit_id, slot=slot,
                                   kind=classify(before, after), source='observed',
                                   window_start=before['observed_at'], window_end=after['observed_at'],
                                   used_before=before['used_percent'], used_after=after['used_percent'],
                                   resets_at_before=before['resets_at'], resets_at_after=after['resets_at'],
                                   note=None))
    return events


def announced(announcements):
    """Announcements as account-wide early events: a point window, no bucket."""
    return [dict(provider='codex', limit_id=None, slot=None, kind='early', source='announcement',
                 window_start=item['at'], window_end=item['at'], used_before=None, used_after=None,
                 resets_at_before=None, resets_at_after=None, note=item['note'])
            for item in announcements]


def same_reset(before, after):
    # The server's resetsAt drifts by a second between reads; that is not a new value.
    if before is None or after is None:
        return before == after
    return abs((parse_time(after) - parse_time(before)).total_seconds()) <= RESET_TOLERANCE_SECONDS


def failure_reason(error):
    # Exception text can carry app-server stderr or account payloads: keep a category only.
    if 'timed out' in str(error):
        return 'timeout'
    if isinstance(error, FileNotFoundError):
        return 'codex executable not found'
    if isinstance(error, OSError):
        return 'app-server unavailable'
    return 'invalid or failed response'


def sample(store, reader, clock, events=None):
    now = clock()
    try:
        records = normalize(reader(), now)
    except (ValueError, OSError, KeyError, TypeError, OverflowError,
            subprocess.TimeoutExpired) as error:
        records = [failed(now, failure_reason(error))]
    if store.append(records, now) and events is not None:
        # Derived from the history alone, so events survive the samples' 30 days.
        events.merge(detect(store.load()), now)
    return records


class Collector:
    """Broker-resident daemon thread. Owns no session, route or message state."""
    def __init__(self, store, interval_minutes=DEFAULT_INTERVAL_MINUTES, executable='codex',
                 reader=None, clock=None):
        if interval_minutes < MIN_INTERVAL_MINUTES:
            raise ValueError(f'Usage interval must be at least {MIN_INTERVAL_MINUTES} minutes')
        self.store, self.interval = store, interval_minutes * 60
        self.events = EventStore(store.directory)
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
            records = sample(self.store, self.reader, self.clock, self.events)
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


def width(event):
    return (parse_time(event['window_end']) - parse_time(event['window_start'])).total_seconds()


def midpoint(event):
    return parse_time(event['window_start']) + timedelta(seconds=width(event) / 2)


def intervals(events):
    """Hours between consecutive early events of ONE source; a too-wide window breaks the chain."""
    early = sorted((e for e in events if e['kind'] == 'early'), key=midpoint)
    return [(midpoint(b) - midpoint(a)).total_seconds() / 3600 for a, b in zip(early, early[1:])
            if width(a) <= MAX_EVENT_WINDOW_SECONDS and width(b) <= MAX_EVENT_WINDOW_SECONDS]


def median(values):
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def estimate(observed, announcements, now):
    """Next early reset from one source's intervals, anchored at the latest early event.

    Observed intervals are used once there are MIN_INTERVALS of them, otherwise the
    announcements'; the two are never pooled. Returns state 'too_few' with both counts
    when neither source has enough, and never a time in that case.
    """
    counts = dict(observed=len(intervals(observed)), announcement=len(intervals(announcements)))
    source = next((name for name in ('observed', 'announcement') if counts[name] >= MIN_INTERVALS), None)
    if source is None:
        return dict(state='too_few', counts=counts)
    hours = intervals(observed if source == 'observed' else announcements)
    early = sorted((e for e in observed + announcements if e['kind'] == 'early'), key=midpoint)
    latest = early[-1]
    if width(latest) > MAX_EVENT_WINDOW_SECONDS:
        return dict(state='no_anchor', counts=counts, source=source, n=len(hours))
    anchor = midpoint(latest)
    centre = anchor + timedelta(hours=median(hours))
    low, high = anchor + timedelta(hours=min(hours)), anchor + timedelta(hours=max(hours))
    state = 'lapsed' if now > high else 'overdue' if now > centre else 'ahead'
    return dict(state=state, counts=counts, source=source, n=len(hours),
                mean_hours=sum(hours) / len(hours), median_hours=median(hours),
                min_hours=min(hours), max_hours=max(hours), anchor=utc(anchor),
                anchor_source=latest['source'], centre=utc(centre), low=utc(low), high=utc(high))


def horizons(official, guess):
    """Asymmetric planning times.

    spend: the earlier of the official reset and the estimate's centre, because what is
    saved past an early reset is lost. stop risk: the official resets_at only, because
    an early reset is never guaranteed.
    """
    usable = guess['state'] in ('ahead', 'overdue')
    spend, basis = official, 'official'
    if usable and (official is None or parse_time(guess['centre']) < parse_time(official)):
        spend, basis = guess['centre'], 'estimate'
    return dict(spend_horizon=spend, spend_basis=basis if spend is not None else None,
                stop_risk_horizon=official)


def all_events(store_dir, records):
    """Persisted events plus those derivable from the current samples, de-duplicated."""
    merged, known = [], set()
    for item in EventStore(store_dir).load() + detect(records):
        if event_key(item) not in known:
            known.add(event_key(item))
            merged.append(item)
    return sorted(merged, key=lambda r: (r['window_end'], r['window_start']))


def annotate(summary, events, announcements, now):
    for row in summary['buckets']:
        observed = [e for e in events if (e['limit_id'], e['slot']) == (row['limit_id'], row['slot'])]
        guess = estimate(observed, announcements, now)
        row['early_reset'] = guess
        row.update(horizons(row['resets_at'], guess))
        row['events'] = observed
    return summary


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
        guess = row.get('early_reset') or {}
        if guess.get('state') in ('ahead', 'overdue'):
            parts.append(f"next early reset ~{local(guess['centre'])} "
                         f"[est, n={guess['n']}, {guess['source']}"
                         + (', overdue]' if guess['state'] == 'overdue' else ']'))
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


def hours(value):
    return f'{value:.1f}h'


def format_event(event):
    when = (local(event['window_start']) if event['window_start'] == event['window_end']
            else f"{local(event['window_start'])} … {local(event['window_end'])}")
    line = f"  {event['kind']:<9} {when}"
    if event['used_before'] is not None:
        line += f"  used {event['used_before']:g}% -> {event['used_after']:g}%"
    if event['resets_at_before'] != event['resets_at_after']:
        before = 'unknown' if event['resets_at_before'] is None else local(event['resets_at_before'])
        after = 'unknown' if event['resets_at_after'] is None else local(event['resets_at_after'])
        line += f", resets {before} -> {after}"
    if width(event) > MAX_EVENT_WINDOW_SECONDS:
        line += f"  (window {span(width(event))}, too wide to time)"
    line += f"  [{event['source']}]"
    return line + (f" {event['note']}" if event['note'] else '')


def format_events(summary, announcements):
    lines = [f"announcements ({len(announcements)}):"] + [format_event(e) for e in announcements]
    if not announcements:
        lines = ['announcements: none seeded']
    for row in summary['buckets']:
        name = f"{row['provider']}/{row['limit_id']}" + ('' if row['slot'] == 'primary' else f"/{row['slot']}")
        lines.append(f"{name} {window_name(row['window_mins'])} events ({len(row['events'])}):")
        lines += [format_event(e) for e in row['events']] or ['  none observed']
        guess = row['early_reset']
        counts = guess['counts']
        if guess['state'] == 'too_few':
            lines.append(f"early-reset intervals: too few (observed n={counts['observed']}, "
                         f"announcement n={counts['announcement']}; need {MIN_INTERVALS}) · no estimate")
        elif guess['state'] == 'no_anchor':
            lines.append(f"early-reset intervals [{guess['source']}, n={guess['n']}] · no estimate: "
                         f"the latest early reset's window is too wide to anchor one")
        else:
            lines.append(f"early-reset intervals [{guess['source']}, n={guess['n']}]: "
                         f"mean {hours(guess['mean_hours'])} · median {hours(guess['median_hours'])} · "
                         f"range {hours(guess['min_hours'])}–{hours(guess['max_hours'])}")
            note = dict(ahead='', overdue=' (overdue: centre has passed)',
                        lapsed=' (lapsed: window passed with no early reset; not used)')[guess['state']]
            lines.append(f"next early reset ~{local(guess['centre'])} [est, n={guess['n']}, {guess['source']}] "
                         f"window {local(guess['low'])} … {local(guess['high'])}, "
                         f"from {guess['anchor_source']} reset {local(guess['anchor'])}{note}")
        spend = 'unknown' if row['spend_horizon'] is None else f"{local(row['spend_horizon'])} ({row['spend_basis']})"
        stop = 'unknown' if row['stop_risk_horizon'] is None else f"{local(row['stop_risk_horizon'])} (official resets_at)"
        lines.append(f"spend horizon: {spend}")
        lines.append(f"stop risk horizon: {stop}")
    if not summary['buckets']:
        lines.append('codex: unknown (no successful read recorded)')
    return '\n'.join(lines)


def query(state_dir, since='6h', provider='codex', now=None):
    """Reads the history, event and announcement files; never collects or calls anything."""
    if provider != 'codex':
        raise ValueError('Only the codex provider is collected')
    now = now or datetime.now(timezone.utc)
    directory = Path(state_dir).expanduser() / 'usage'
    records = UsageStore(directory).load()
    announcements = announced(AnnouncementStore(directory).load())
    summary = annotate(summarize(records, now, parse_since(since)), all_events(directory, records),
                       announcements, now)
    summary['announcements'] = announcements
    return records, summary, now


def seed_announcements(state_dir, path, now=None):
    now = now or datetime.now(timezone.utc)
    store = AnnouncementStore(Path(state_dir).expanduser() / 'usage')
    return dict(added=len(store.seed(path, now)), total=len(store.load()), path=str(store.path))
