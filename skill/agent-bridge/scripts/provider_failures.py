"""Provider-neutral retry policy and normalized event bodies. No runtime I/O."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import re

DETECTED_EVENT = 'provider_failure_detected'
RETRY_EVENT = 'provider_retry'
EXHAUSTED = 'provider_retry_exhausted'
UNSAFE = 'provider_retry_unsafe'
UNCERTAIN = 'provider_retry_uncertain'
DEFAULT_BACKOFF = (15, 30, 60)
DEFAULT_CAP = 3


@dataclass(frozen=True)
class RetryPolicy:
    backoff: tuple[int, ...]
    cap: int


def retry_policy(backoff=DEFAULT_BACKOFF, cap=DEFAULT_CAP):
    if type(cap) is not int or cap < 1:
        raise ValueError('Retry cap must be an integer >= 1')
    if (not isinstance(backoff, tuple) or not backoff
            or any(type(delay) is not int or delay <= 0 for delay in backoff)):
        raise ValueError('Retry backoff must be a nonempty tuple of positive integers')
    return RetryPolicy(backoff=backoff, cap=cap)


def next_delay(policy, attempts_done):
    if type(attempts_done) is not int or attempts_done < 0:
        raise ValueError('Accepted attempt count must be a nonnegative integer')
    if attempts_done >= policy.cap:
        return None
    return policy.backoff[min(attempts_done, len(policy.backoff) - 1)]


def retry_event_body(event, *, provider, kind, failed_at, correlation, attempt=None, cap=None):
    if event not in (DETECTED_EVENT, RETRY_EVENT, EXHAUSTED, UNSAFE, UNCERTAIN):
        raise ValueError('Unknown provider retry event')
    if provider not in ('codex', 'agy'):
        raise ValueError('Unknown provider')
    if not isinstance(kind, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,127}', kind):
        raise ValueError('A normalized failure kind is required')
    if (not isinstance(correlation, str) or not 1 <= len(correlation) <= 256
            or any(character.isspace() for character in correlation)):
        raise ValueError('A correlation ID is required')
    if failed_at is not None:
        if not isinstance(failed_at, str) or len(failed_at) > 40:
            raise ValueError('Failure time must be an ISO-8601 UTC string or None')
        try:
            parsed = datetime.fromisoformat(failed_at.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('Failure time must be an ISO-8601 UTC string or None') from None
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError('Failure time must be UTC')
    if event in (RETRY_EVENT, EXHAUSTED):
        if type(attempt) is not int or type(cap) is not int or not 1 <= attempt <= cap:
            raise ValueError('A valid attempt k/N is required')
        if event == EXHAUSTED and attempt != cap:
            raise ValueError('Exhaustion requires the cap attempt')
    elif attempt is not None or cap is not None:
        raise ValueError('Attempt and cap apply only to retry and exhausted events')
    return dict(sorted(dict(event=event, provider=provider, kind=kind, failed_at=failed_at,
                            correlation=correlation, attempt=attempt, cap=cap).items()))
