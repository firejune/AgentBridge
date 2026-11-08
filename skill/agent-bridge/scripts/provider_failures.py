"""Read-only Codex failure observation. Normalized notices contain no diagnostics."""
import asyncio
from datetime import datetime, timezone
import math

import codex_live

DEFAULT_INTERVAL = 10
DETECTED_EVENT = 'provider_failure_detected'
TRANSIENT_FAILURES = frozenset(('responseStreamConnectionFailed', 'responseStreamDisconnected',
                                'responseTooManyFailedAttempts', 'internalServerError'))


def transient_kind(info):
    if isinstance(info, str):
        return info if info in TRANSIENT_FAILURES else None
    if not isinstance(info, dict) or len(info) != 1:
        return None
    kind, details = next(iter(info.items()))
    if kind not in TRANSIENT_FAILURES | {'httpConnectionFailed'} or not isinstance(details, dict):
        return None
    status = details.get('httpStatusCode')
    if status is None:
        return kind
    if type(status) is int and (status in (408, 429) or 500 <= status <= 599):
        return kind
    return None


def latest_turn(snapshot):
    history = snapshot.get('turnHistory')
    if isinstance(history, dict) and history.get('kind') == 'canonical':
        history = history.get('history', {})
        # Only an island with an exhausted newer boundary proves the live tail.
        tails = [island for island in history.get('islands', [])
                 if island.get('newerBoundary', {}).get('status') == 'exhausted']
        if len(tails) != 1 or not tails[0].get('entries'):
            return None
        key = tails[0]['entries'][-1].get('value')
        return history.get('entitiesByKey', {}).get(key)
    turns = snapshot.get('turns')
    if snapshot.get('resumeState') == 'resumed' and isinstance(turns, list) and turns:
        return turns[-1]
    return None


def terminal_failure(snapshot):
    """Require both the owner's terminal error item and failed completion state."""
    turn = latest_turn(snapshot)
    if (snapshot.get('threadRuntimeStatus', {}).get('type') != 'idle'
            or not isinstance(turn, dict) or not isinstance(turn.get('turnId'), str)
            or not 1 <= len(turn['turnId']) <= 256
            or turn.get('status') != 'failed'):
        return None
    items = turn.get('items')
    if not isinstance(items, list):
        return None
    errors = [item for item in items if isinstance(item, dict) and item.get('type') == 'error']
    if not errors or errors[-1].get('willRetry') is not False:
        return None
    kind = transient_kind(errors[-1].get('errorInfo'))
    error = turn.get('error')
    if not kind or not isinstance(error, dict) or transient_kind(error.get('codexErrorInfo')) != kind:
        return None
    # Snapshot errors have no individual timestamp. Derive the completed-turn
    # time only when both timing fields exist, otherwise leave it unavailable.
    start, duration = turn.get('turnStartedAtMs'), turn.get('durationMs')
    failed_at = None
    if all(type(value) in (int, float) and math.isfinite(value) and value >= 0
           for value in (start, duration)):
        seconds = (start + duration) / 1000
        if 0 <= seconds <= 253402300799:
            failed_at = datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    return dict(kind=kind, failed_at=failed_at, turn_id=turn['turnId'])


def read_codex_failure(session):
    with codex_live.Client(timeout=3) as client:
        owner = client.owner(session)
        snapshot = client.snapshot(session, owner)
        return terminal_failure(snapshot)


def last_sender_message(state, route):
    notices = {value[1] for key, value in state.keys.items()
               if key.startswith(DETECTED_EVENT + ':')}
    for message in reversed(list(state.messages.values())):
        if (message['id'] not in notices and all(message['route'].get(field) == route.get(field)
                for field in ('project', 'name', 'transport', 'session', 'cwd', 'executable'))):
            return message
    return None


async def observe_codex_failures(state, interval):
    """Read each registered Codex thread and notify its last retained sender."""
    while True:
        await asyncio.sleep(interval)
        for route in list(state.endpoints.values()):
            if route['transport'] != 'codex':
                continue
            source = last_sender_message(state, route)
            source_id = source['id'] if source else None
            try:
                failure = await asyncio.to_thread(read_codex_failure, route['session'])
                if failure is not None:
                    state.action(dict(op='provider-failure', expected_route=route,
                                      source_message_id=source_id, failure=failure))
            except (ValueError, OSError, TypeError, KeyError, AttributeError):
                # Transport loss or unsupported state is never a provider fault.
                continue
