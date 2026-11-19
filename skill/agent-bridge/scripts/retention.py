"""Provider-neutral retention planner. No I/O, clocks, dispatch or diagnostics."""
from collections import Counter
from datetime import datetime
import hashlib
import json
import math

BODY_TTL = 24 * 60 * 60
LOOKUP_TTL = 7 * 24 * 60 * 60
MAX_COMPACT = 8192
MAX_COMPACT_BYTES = 16384
TERMINAL = frozenset(('done', 'answered', 'failed'))
PUSH_TRANSPORTS = frozenset(('codex', 'claude-peer', 'agy'))
INTENT_FIELDS = ('project', 'sender', 'recipient', 'channel', 'sender_channel', 'body', 'key', 'reply_to')


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def fingerprint(intent):
    # Include nullable historical fields exactly as old callers did. Optional new
    # fields have canonical defaults so pre-upgrade idempotency rows still match.
    normalized = {field: intent.get(field) for field in INTENT_FIELDS}
    normalized['one_way'] = intent.get('one_way', False)
    normalized['reply_deadline_seconds'] = intent.get('reply_deadline_seconds')
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def matches(stored, incoming):
    return stored.get('fingerprint', fingerprint(stored)) == fingerprint(incoming)


def request_status(message, current):
    deadline = message.get('reply_deadline_at')
    if deadline is None:
        return {}
    completed = message.get('completed_at')
    last_reply = message.get('last_reply_at', completed)
    late = (valid_time(last_reply) and last_reply > deadline
            and message['state'] == 'answered')
    expired = current >= deadline and not (valid_time(completed) and completed <= deadline)
    return {'request_status': 'expired' if expired else ('answered' if message['state'] == 'answered' else
                'completed' if message['state'] == 'done' else 'open'),
            'late_reply': bool(late)}


def provider_sources(messages, compact):
    return {control.get('source_message_id')
            for message in (*messages.values(), *compact.values())
            if (control := message.get('provider_retry')) is not None
            and control['status'] != 'recovered'}


def removal_protection(message, waiters, sources, parent=None):
    mid = message['id']
    if mid == parent or waiters.get(mid) or waiters.get(message.get('reply_to')):
        return 'active_wait_or_reply'
    if (message.get('provider_retry', {}).get('status', 'recovered') != 'recovered'
            or mid in sources):
        return 'provider_episode'
    return None


def age_removable(message, waiters, sources, current, parent=None):
    created = timestamp(message.get('created'))
    return (message['state'] in ('submitted', 'received') and created is not None
            and current - created >= BODY_TTL
            and removal_protection(message, waiters, sources, parent) is None)


def plan(messages, compact, waiters, current, limit, parent=None):
    """Return IDs privately; callers expose only ``counts``. Never modifies inputs."""
    adopt, candidates, reasons, anchors, remove = {}, [], Counter(), {}, []
    sources = provider_sources(messages, compact)
    # A retained answer must remain observable until explicitly completed. Its
    # ancestor stays intact too; a wait may attach after the answer was posted.
    children = {}
    for message in messages.values():
        if message.get('reply_to'):
            children.setdefault(message['reply_to'], []).append(message)
    for mid, message in messages.items():
        reason = None
        state = message['state']
        created = timestamp(message.get('created'))
        if state in ('submitted', 'received') and created is not None and current - created >= BODY_TTL:
            # These two states have a direct age limit, independent of receipt
            # capacity, completion, ACKs, deadlines or optional reply retention.
            reason = removal_protection(message, waiters, sources, parent)
            if reason:
                reasons[reason] += 1
            else:
                remove.append(mid)
            continue
        one_way_delivered = (message.get('one_way') is True
            and (state == 'received' or state == 'submitted'
                 and message['route']['transport'] in PUSH_TRANSPORTS))
        expired_delivered = (message.get('reply_deadline_at', float('inf')) <= current
            and state in ('submitted', 'received')
            and valid_time(message.get('delivered_at')))
        if mid == parent or waiters.get(mid):
            reason = 'active_wait_or_reply'
        elif state in ('pending', 'dispatching', 'uncertain', 'in_progress'):
            reason = 'active_or_uncertain'
        elif message.get('provider_retry', {}).get('status', 'recovered') != 'recovered':
            reason = 'provider_episode'
        elif any(child['state'] not in TERMINAL or waiters.get(child['id']) for child in children.get(mid, ())):
            reason = 'reply_observation'
        elif message.get('reply_to') in messages and (
                messages[message['reply_to']]['state'] not in TERMINAL
                or waiters.get(message['reply_to'])):
            reason = 'ancestor_active'
        elif state not in ('done', 'answered') and not one_way_delivered and not expired_delivered:
            reason = 'no_completion_evidence'
        if reason:
            reasons[reason] += 1
            continue
        since = message.get('completed_at') if state in ('done', 'answered') else (
            max(message['reply_deadline_at'], message['delivered_at']) if expired_delivered else message.get('delivered_at'))
        if not valid_time(since):
            # Legacy checkpoints discarded transition events. A correlated reply
            # cannot predate the answer, so its creation is a conservative bound.
            reply_times = [timestamp(m['created']) for m in children.get(mid, ())]
            reply_times = [value for value in reply_times if value is not None]
            ack_time = timestamp(message.get('acknowledged_at')) if state == 'done' else None
            since = (min(reply_times) if state == 'answered' and reply_times else
                     ack_time if ack_time is not None else message.get('retention_adopted_at', current))
            if 'retention_adopted_at' not in message:
                adopt[mid] = since
        if current - since < BODY_TTL:
            reasons['body_ttl'] += 1
        elif len(json.dumps(compact_message(message, current, since), ensure_ascii=False).encode()) + 1 > MAX_COMPACT_BYTES:
            reasons['compact_record_too_large'] += 1
        else:
            candidates.append(mid)
            anchors[mid] = since
    # Never discard compact metadata before its own TTL to admit another body.
    drop = [mid for mid, message in compact.items()
            if message['lookup_expires_at'] <= current and not waiters.get(mid)
            and mid != parent and mid not in children]
    available = MAX_COMPACT - len(compact) + len(drop)
    selected = candidates[:max(0, available)]
    if len(selected) < len(candidates):
        reasons['compact_capacity'] += len(candidates) - len(selected)
    counts = dict(active_messages=len(messages), compact_receipts=len(compact),
                  would_compact=len(selected), would_forget=len(drop), would_remove=len(remove),
                  legacy_adoptions=len(adopt), protected=dict(sorted(reasons.items())),
                  free_slots_after=max(0, limit - len(messages) + len(selected) + len(remove)),
                  admission_blocked=len(messages) - len(selected) - len(remove) >= limit,
                  body_ttl_seconds=BODY_TTL, lookup_ttl_seconds=LOOKUP_TTL,
                  compact_limit=MAX_COMPACT, compact_record_bytes_limit=MAX_COMPACT_BYTES)
    return dict(adopt=adopt, compact=selected, drop=drop, remove=remove, counts=counts,
                anchors={mid: anchors[mid] for mid in selected})


def compact_message(message, current, anchor):
    result = {key: value for key, value in message.items()
              if key not in ('body', 'note', 'events', 'dispatch_claim', 'dispatch_claim_deadline')}
    body = message['body']
    result.update(body='', body_expired=True, body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                  body_chars=len(body), body_bytes=len(body.encode()), compacted_at=current,
                  body_ttl_anchor_at=anchor,
                  lookup_expires_at=current + LOOKUP_TTL, note=None, events=[])
    return result
