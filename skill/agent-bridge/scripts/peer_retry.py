"""claude-peer wake evidence and closed-inbox retries (issue #121).

A push to an idle-closed Claude session wakes it once inside the sender's own command. Before
#121 the wake outcome lived only in the in-memory note and events, which the correspondence
checkpoint never keeps, and a message that stayed `pending` was never tried again. Here:

- every wake outcome a push reports is kept on the message (`wakes`, newest last) and survives
  restarts, answers and completion for as long as the message is retained;
- a `pending` claude-peer message whose last push failed with a closed inbox (`failure: closed`:
  nothing was written) is pushed again by the broker on a bounded backoff with a cap. Each retry
  is one ordinary push: one guarded claim, at most one wake, at most one write. Any other end
  (`uncertain`, a wake that may have reached the app, an interrupt, a protocol error) stops the
  retries for good.
"""
import asyncio
from copy import deepcopy
import json
import math
import re
import sys
import time

import provider_failures

WAKE_STATES = frozenset(('woken', 'already-live', 'unsupported', 'failed', 'uncertain'))
WAKE_SOURCES = frozenset(('send', 'retry'))
MAX_WAKES = 10
REASON_CHARS = 500
WAKE_URL = re.compile(r'claude://claude\.ai/epitaxy/local_[0-9A-Fa-f-]{1,64}')
DESKTOP_SESSION = re.compile(r'local_[0-9A-Fa-f-]{1,64}')

FAILURES = frozenset(('closed', 'wake-uncertain', 'interrupted', 'error'))
STATUSES = frozenset(('waiting', 'retrying', 'exhausted', 'stopped', 'delivered'))
DEFAULT_INTERVAL = 15  # seconds between scans for due retries (0 = off)
DEFAULT_BACKOFF = (30, 60, 120, 300, 600, 900, 1800)  # seconds before retry k; the last repeats
DEFAULT_CAP = 16  # retries after the first failed push (about five hours with the default backoff)


def short(value, limit=REASON_CHARS):
    return value if len(value) <= limit else value[:limit - 1] + '…'


def wake_record(raw, *, strict):
    """A wake outcome without endpoints or credentials. strict=False coerces a client report."""
    def bad(what):
        if strict:
            raise ValueError(f'Invalid wake record {what}')
        return None
    if not isinstance(raw, dict):
        raise ValueError('Invalid wake record')
    state = raw.get('state')
    if state not in WAKE_STATES:
        raise ValueError('Invalid wake state')
    record = {'state': state}
    reason = raw.get('reason')
    record['reason'] = short(reason) if isinstance(reason, str) else bad('reason')
    if strict and record['reason'] is not None and len(record['reason']) > REASON_CHARS:
        raise ValueError('Invalid wake record reason')
    url = raw.get('url')
    record['url'] = url if isinstance(url, str) and WAKE_URL.fullmatch(url) else (None if url is None else bad('url'))
    host = raw.get('desktop_session')
    record['desktop_session'] = (host if isinstance(host, str) and DESKTOP_SESSION.fullmatch(host)
                                 else (None if host is None else bad('desktop session')))
    entry = raw.get('host')
    record['host'] = short(entry, 64) if isinstance(entry, str) else (None if entry is None else bad('host'))
    elapsed = raw.get('elapsed')
    record['elapsed'] = (elapsed if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0
                         else (None if elapsed is None else bad('elapsed')))
    at = raw.get('at')
    record['at'] = at if isinstance(at, str) and len(at) <= 40 else (None if at is None else bad('time'))
    source = raw.get('source', 'send')
    record['source'] = source if source in WAKE_SOURCES else bad('source')
    attempt = raw.get('attempt', 0)
    record['attempt'] = attempt if type(attempt) is int and 0 <= attempt <= 1000 else bad('attempt')
    if strict and set(raw) - set(record):
        raise ValueError('Invalid wake record fields')
    return record


def wake_records(raw):
    """Restore kept wake outcomes; a malformed entry is dropped, never the whole checkpoint."""
    if not isinstance(raw, list):
        return []
    kept = []
    for item in raw[-MAX_WAKES:]:
        try:
            kept.append(wake_record(item, strict=True))
        except (ValueError, TypeError):
            continue
    return kept


def retry_record(raw):
    """Restore a retry schedule; anything malformed is dropped (which only ever stops retries)."""
    fields = {'status', 'attempts', 'cap', 'next_at', 'failure', 'reason'}
    if not isinstance(raw, dict) or set(raw) != fields:
        return None
    if (raw['status'] not in STATUSES or type(raw['attempts']) is not int or type(raw['cap']) is not int
            or not 0 <= raw['attempts'] <= raw['cap'] <= 1000
            or (raw['failure'] is not None and raw['failure'] not in FAILURES)
            or (raw['reason'] is not None and (not isinstance(raw['reason'], str) or len(raw['reason']) > REASON_CHARS))):
        return None
    due = raw['next_at']
    if due is not None and (type(due) not in (int, float) or not math.isfinite(due) or due < 0):
        return None
    return dict(raw)


def restored(message):
    """The broker stopped during a retry. A claim restores as `uncertain` (never retried);
    a message that is still pending was never claimed, so it may wait for its slot again."""
    retry = message.get('peer_retry')
    if retry is not None and retry['status'] == 'retrying':
        message['peer_retry'] = dict(retry, status='waiting', next_at=time.time())


class PeerRetries:
    """Broker-side bookkeeping. Every method runs on the event loop; pushes run in a worker thread."""

    def __init__(self, policy=None):
        self.policy = policy or provider_failures.retry_policy(DEFAULT_BACKOFF, DEFAULT_CAP)
        self.inflight = set()

    def record_wake(self, message, raw):
        """Keep a reported wake outcome on the message; a malformed report is kept as its state only."""
        if message['route']['transport'] != 'claude-peer' or raw is None:
            return
        try:
            record = wake_record(raw, strict=False)
        except (ValueError, TypeError):
            return
        if message['id'] in self.inflight:
            record.update(source='retry', attempt=message['peer_retry']['attempts'])
        message['wakes'] = (message.get('wakes', []) + [record])[-MAX_WAKES:]

    def settle(self, message, state, failure, note):
        """Update the schedule after a finish of a claude-peer delivery."""
        if message['route']['transport'] != 'claude-peer':
            return
        retry = message.get('peer_retry')
        if state == 'submitted':
            if retry is not None:
                message['peer_retry'] = dict(retry, status='delivered', next_at=None)
            return
        if state == 'pending' and failure == 'closed':
            if retry is None:
                retry = dict(status='waiting', attempts=0, cap=self.policy.cap, next_at=None,
                             failure=None, reason=None)
            if retry['status'] == 'stopped':
                return  # Never resumed after an uncertain or unsafe end.
            delay = provider_failures.next_delay(self.policy, min(retry['attempts'], self.policy.cap))
            message['peer_retry'] = dict(retry, failure='closed', reason=short(note) if note else None,
                                         status='waiting' if delay is not None else 'exhausted',
                                         next_at=time.time() + delay if delay is not None else None)
            return
        if retry is not None or state == 'uncertain' or failure in ('wake-uncertain', 'interrupted'):
            # Any other end stops retries for good: a possible write, a wake that may have
            # reached the app, an interrupt or a non-inbox error.
            cause = failure if state == 'pending' and failure in FAILURES else state
            base = retry or dict(attempts=0, cap=self.policy.cap)
            message['peer_retry'] = dict(status='stopped', attempts=base['attempts'], cap=base['cap'],
                                         next_at=None, failure=cause if cause in FAILURES else None,
                                         reason=short(f'stopped after {cause}' + (f': {note}' if note else '')))

    def due(self, state, moment):
        blockers = state.delivery_blockers()
        for message in list(state.messages.values()):
            retry = message.get('peer_retry')
            if (message['state'] == 'pending' and message['route']['transport'] == 'claude-peer'
                    and retry is not None and retry['status'] == 'waiting'
                    and retry['next_at'] is not None and retry['next_at'] <= moment
                    and message['id'] not in self.inflight
                    and not state.pending_delivery(message, blockers)):
                yield message['id']

    def begin(self, state, mid):
        """Durably count one retry before it runs. Returns the message to push, or None."""
        message = state.messages.get(mid)
        retry = message and message.get('peer_retry')
        if (not retry or message['state'] != 'pending' or retry['status'] != 'waiting'
                or retry['attempts'] >= retry['cap']):
            return None
        before = retry
        message['peer_retry'] = dict(retry, status='retrying', attempts=retry['attempts'] + 1, next_at=None)
        if state.correspondence is not None:
            try:
                state.correspondence.save(state)
            except (OSError, ValueError):
                message['peer_retry'] = before
                raise
        self.inflight.add(mid)
        copy = deepcopy(message)
        for field in ('dispatch_claim', 'dispatch_claim_deadline'):
            copy.pop(field, None)
        return copy

    def end(self, state, mid):
        """After a retry returned: a push that never reached finish waits for its next slot."""
        self.inflight.discard(mid)
        message = state.messages.get(mid)
        retry = message and message.get('peer_retry')
        if not retry or retry['status'] != 'retrying':
            return
        delay = provider_failures.next_delay(self.policy, retry['attempts'])
        message['peer_retry'] = dict(retry, status='waiting' if delay is not None else 'exhausted',
                                     next_at=time.time() + delay if delay is not None else None)
        if state.correspondence is not None:
            state.correspondence.save(state)


async def observe(state, path, pusher, interval):
    """Push due retries one at a time, so at most one wake runs from here at any moment."""
    retries = state.peer_retries
    while True:
        await asyncio.sleep(interval)
        for mid in list(retries.due(state, time.time())):
            try:
                message = retries.begin(state, mid)
            except (OSError, ValueError) as error:
                print(json.dumps(dict(peer_retry=mid, skipped=type(error).__name__)), file=sys.stderr, flush=True)
                continue
            if message is None:
                continue
            outcome = 'submitted'
            try:
                await asyncio.to_thread(pusher, str(path), message)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # The push settled the message itself; only log its class.
                outcome = type(error).__name__
            finally:
                try:
                    retries.end(state, mid)
                except (OSError, ValueError):
                    pass
            print(json.dumps(dict(peer_retry=mid, attempt=message['peer_retry']['attempts'], outcome=outcome)),
                  file=sys.stderr, flush=True)
