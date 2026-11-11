"""claude-peer wake evidence kept with a message (issue #121).

A push to an idle-closed Claude session wakes it once inside the sender's own command. The
correspondence checkpoint never keeps notes or events, so before #121 a broker restart erased
the wake outcome. Here each outcome a push reports is kept on the message as `wakes`, newest
last, in normalized form only: state, a fixed reason code, elapsed, at, source and attempt.
No URL, path, stderr or free text: the full `reason` stays in the volatile note/event stream.
Nothing here schedules, wakes or delivers anything.
"""
import math

WAKE_STATES = frozenset(('woken', 'already-live', 'unsupported', 'failed', 'uncertain'))
WAKE_CODES = frozenset(('woken', 'already_live', 'inbox_off', 'live_unreachable', 'ide_host',
                        'no_desktop_session', 'ambiguous_desktop_session', 'archived', 'scheduled_task',
                        'budget_spent', 'opener_failed', 'opener_error', 'record_timeout', 'inbox_timeout',
                        'host_mismatch', 'unknown'))
SOURCES = frozenset(('push',))
FIELDS = frozenset(('state', 'code', 'elapsed', 'at', 'source', 'attempt'))
MAX_WAKES = 10
MAX_ATTEMPT = 1_000_000


def member(value, allowed):
    """`value in allowed` that is False (never TypeError) for unhashable values."""
    return isinstance(value, str) and value in allowed


def elapsed_value(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 86400 else None


def time_value(value):
    return value if isinstance(value, str) and 0 < len(value) <= 40 else None


def normalized(raw, attempt):
    """Reduce a push's wake report to the kept fields, or None when its state is not a wake state."""
    if not isinstance(raw, dict) or not member(raw.get('state'), WAKE_STATES):
        return None
    code = raw.get('code')
    return {'state': raw['state'], 'code': code if member(code, WAKE_CODES) else 'unknown',
            'elapsed': elapsed_value(raw.get('elapsed')), 'at': time_value(raw.get('at')),
            'source': 'push', 'attempt': attempt}


def record_wake(message, raw):
    """Keep one reported wake outcome on a claude-peer message (on the broker's event loop)."""
    if message['route']['transport'] != 'claude-peer':
        return
    kept = message.get('wakes', [])
    record = normalized(raw, len(kept) + 1)
    if record is not None:
        message['wakes'] = (kept + [record])[-MAX_WAKES:]


def restored(raw):
    """One checkpoint entry exactly as kept, or None. Never raises, whatever the entry holds."""
    try:
        if (not isinstance(raw, dict) or set(raw) != FIELDS
                or not member(raw['state'], WAKE_STATES) or not member(raw['code'], WAKE_CODES)
                or not member(raw['source'], SOURCES)
                or type(raw['attempt']) is not int or not 1 <= raw['attempt'] <= MAX_ATTEMPT
                or (raw['elapsed'] is not None and elapsed_value(raw['elapsed']) is None)
                or (raw['at'] is not None and time_value(raw['at']) is None)):
            return None
        return {field: raw[field] for field in sorted(FIELDS)}
    except Exception:  # noqa: BLE001 - evidence must never cost the correspondence
        return None


def wake_records(raw):
    """Restore kept wake outcomes; a malformed entry is dropped alone, never the checkpoint."""
    if not isinstance(raw, list):
        return []
    return [record for record in map(restored, raw[-MAX_WAKES:]) if record is not None]
