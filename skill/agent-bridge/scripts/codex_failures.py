"""Codex failure observation and guarded native Resume continuations."""
import asyncio
from datetime import datetime, timezone
import math
import hashlib
import json
import concurrent.futures
import time
import uuid
import threading

import codex_live
import provider_failures
import message_store

DEFAULT_INTERVAL = 10
DETECTED_EVENT = provider_failures.DETECTED_EVENT
TRANSIENT_FAILURES = frozenset(('responseStreamConnectionFailed', 'responseStreamDisconnected',
                                'responseTooManyFailedAttempts', 'internalServerError'))


class RetryRefused(ValueError):
    """The fresh Desktop snapshot cannot authorize a continuation."""


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


def read_codex_observation(session):
    with codex_live.Client(timeout=3) as client:
        owner = client.owner(session)
        return dict(owner=owner, snapshot=client.snapshot(session, owner))


def snapshot_fingerprint(snapshot):
    value = dict(turn=latest_turn(snapshot), requests=snapshot.get('requests'),
                 latestTurnStartMessageId=snapshot.get('latestTurnStartMessageId'),
                 queuedMessages=snapshot.get('queuedMessages'),
                 unconfirmedTurnSubmissions=snapshot.get('unconfirmedTurnSubmissions'))
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def retry_refusal(snapshot, turn_id, fingerprint):
    turn = latest_turn(snapshot)
    if not isinstance(turn, dict) or turn.get('turnId') != turn_id:
        return 'newer_turn_or_missing_history'
    if terminal_failure(snapshot) is None:
        return 'native_retry_or_nonterminal_turn'
    if (snapshot.get('requests') != [] or snapshot.get('unconfirmedTurnSubmissions')
            or snapshot.get('queuedMessages')):
        return 'pending_input_or_native_retry'
    if turn.get('itemsPagination', {}).get('hasLoadedOldest') is False:
        return 'incomplete_tool_history'
    completed = set(turn.get('completedItemIds', []))
    outputs = {item.get('call_id', item.get('callId')) for item in turn['items']
               if item.get('type') in ('function_call_output', 'custom_tool_call_output',
                                       'functionCallOutput', 'customToolCallOutput')}
    passive = {'error', 'agentMessage', 'userMessage', 'reasoning', 'plan', 'todo-list',
               'modelChanged', 'modelRerouted', 'contextCompaction', 'functionCallOutput',
               'customToolCallOutput', 'function_call_output', 'custom_tool_call_output'}
    for item in turn['items']:
        if item.get('type') in passive:
            continue
        if item.get('type') in ('function_call', 'custom_tool_call'):
            call_id = item.get('call_id', item.get('callId'))
            if call_id and call_id in outputs:
                continue
            return 'started_without_output'
        if (item.get('id') not in completed
                and item.get('status') not in ('completed', 'failed', 'declined', 'cancelled')):
            return 'started_without_output'
    if snapshot_fingerprint(snapshot) != fingerprint:
        return 'intervening_input_or_changed_turn'
    return None


def resume_failed_turn(session, owner, turn_id, fingerprint, reserve):
    """Recheck the pinned owner and snapshot, durably reserve, then write once."""
    client = codex_live.Client(timeout=10)
    try:
        with client:
            if client.owner(session) != owner:
                raise RetryRefused('desktop_owner_changed')
            snapshot = client.snapshot(session, owner)
            refusal = retry_refusal(snapshot, turn_id, fingerprint)
            if refusal:
                raise RetryRefused(refusal)
            reserve()
            response = client.call('thread-follower-start-turn', dict(
                conversationId=session, turnStart=dict(
                    request=dict(threadId=session, input=[], turnTrigger='resume_interrupted_task'),
                    context=dict(inheritThreadSettings=True, writingBlockContextPrepared=True))),
                2, owner, mutation=True)
            if response.get('resultType') != 'success':
                raise codex_live.DeliveryError('Codex continuation outcome was not confirmed', True)
            accepted = codex_live.accepted_turn(response)
            if accepted == turn_id:
                raise codex_live.DeliveryError('Codex continuation returned the failed turn ID', True)
            return accepted
    except codex_live.DeliveryError as error:
        error.uncertain = error.uncertain or client.mutation_sent
        raise
    except (OSError, KeyError, TypeError):
        raise codex_live.DeliveryError('Codex continuation transport or payload failed',
                                       client.mutation_sent) from None


def last_sender_message(state, route):
    notices = {value[1] for key, value in state.keys.items()
               if key.startswith(tuple(event + ':' for event in (
                   DETECTED_EVENT, provider_failures.RETRY_EVENT, provider_failures.EXHAUSTED,
                   provider_failures.UNSAFE, provider_failures.UNCERTAIN)))}
    for message in reversed(list(state.messages.values())):
        if (message['id'] not in notices and all(message['route'].get(field) == route.get(field)
                for field in ('project', 'name', 'transport', 'session', 'cwd', 'executable'))):
            return message
    return None


def retry_notice(state, route):
    for message in reversed(list(state.messages.values())):
        if (message.get('provider_retry') is not None
                and message['return_route']['transport'] == route['transport']
                and message['return_route']['session'] == route['session']):
            return message
    return None


def retry_event(state, notice, control, event, *, kind=None, attempt=None):
    body = provider_failures.retry_event_body(event, provider=notice['return_route']['transport'],
        kind=kind or control['kind'], failed_at=control['failed_at'],
        correlation=control['turn_id'], attempt=attempt,
        cap=control['cap'] if attempt is not None else None)
    identity = [notice['id'], event, control['turn_id'], control['reservation_id'], attempt]
    key = event + ':' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    message = state.provider_notice(notice['return_route'], notice['route'],
        json.dumps(body, sort_keys=True), key, event,
        conversation=notice['conversation'], protect=notice['id'])
    return message['id']


def retry_action(state, args):
    """Executed by State.action's durable transaction, before any IPC write."""
    notice = state.get(args['id'])
    action = args['action']
    control = dict(notice['provider_retry']) if notice.get('provider_retry') else None
    at = args.get('at', time.time())
    if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
        raise ValueError('Invalid retry clock')
    if action == 'arm':
        if control is not None:
            return notice
        body = json.loads(notice['body'])
        route = notice['return_route']
        source = last_sender_message(state, route)
        if (body.get('event') != DETECTED_EVENT or body.get('provider') != route['transport']
                or route['transport'] not in ('codex', 'agy')
                or state.endpoints.get((route['project'], route['name'])) != route
                or (source['id'] if source else None) != body['source_message_id']):
            raise RetryRefused('intervening_input_or_route_changed')
        policy = provider_failures.retry_policy(tuple(args['backoff']), args['cap'])
        control = dict(version=1, status='waiting', attempts=0, backoff=list(policy.backoff), cap=policy.cap,
                       turn_id=body['turn_id'], fingerprint=args['fingerprint'], kind=body['kind'],
                       failed_at=body['failed_at'], source_message_id=body['source_message_id'],
                       next_at=at + provider_failures.next_delay(policy, 0), reservation_id=None, event_id=None)
    elif control is None:
        raise ValueError('Provider notice has no retry state')
    elif action == 'wait':
        failure = args['failure']
        transport = notice['return_route']['transport']
        if (control['status'] != 'accepted'
                or (transport == 'codex' and failure['turn_id'] != control['turn_id'])
                or (transport == 'agy' and failure['turn_id'] == control['turn_id'])):
            raise RetryRefused('newer_turn_or_changed_reservation')
        policy = provider_failures.retry_policy(tuple(control['backoff']), control['cap'])
        delay = provider_failures.next_delay(policy, control['attempts'])
        control.update(turn_id=failure['turn_id'], kind=failure['kind'], failed_at=failure['failed_at'],
                       fingerprint=args['fingerprint'], reservation_id=None)
        if delay is None:
            control.update(status='exhausted', next_at=None)
            control['event_id'] = retry_event(state, notice, control, provider_failures.EXHAUSTED,
                                              attempt=control['attempts'])
        else:
            control.update(status='waiting', next_at=at + delay)
    elif action == 'reserve':
        route = notice['return_route']
        source = last_sender_message(state, route)
        provider_ids = {value[1] for key, value in state.keys.items() if key.startswith('provider_')}
        pending_delivery = any(message['route']['transport'] == route['transport']
            and message['route']['session'] == route['session']
            and message['state'] in ('pending', 'dispatching', 'uncertain')
            and message['id'] not in provider_ids for message in state.messages.values())
        if (control['status'] != 'waiting' or at < control['next_at']
                or control['attempts'] >= control['cap']
                or args['fingerprint'] != control['fingerprint']
                or state.endpoints.get((route['project'], route['name'])) != route
                or (source['id'] if source else None) != control['source_message_id']
                or pending_delivery):
            raise RetryRefused('intervening_input_or_route_changed')
        control.update(status='reserved', next_at=None, reservation_id=str(uuid.uuid4()))
        control['event_id'] = retry_event(state, notice, control, provider_failures.RETRY_EVENT,
                                          attempt=control['attempts'] + 1)
    elif action == 'finish':
        if args['reservation_id'] != control['reservation_id']:
            raise RetryRefused('reservation_changed')
        if control['status'] != 'reserved':
            return notice
        if args['outcome'] == 'accepted':
            turn_id = args['turn_id']
            transport = notice['return_route']['transport']
            if (not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256
                    or (transport == 'codex' and turn_id == control['turn_id'])
                    or (transport == 'agy' and turn_id != control['turn_id'])):
                raise ValueError('Invalid accepted continuation turn')
            control.update(status='accepted', attempts=control['attempts'] + 1, turn_id=turn_id)
        else:
            raise ValueError('Invalid retry completion outcome')
    elif action == 'stop':
        event = args['event']
        if event not in (provider_failures.UNSAFE, provider_failures.UNCERTAIN):
            raise ValueError('Invalid terminal retry event')
        if control['status'] in ('unsafe', 'uncertain', 'exhausted', 'recovered'):
            return notice
        control.update(status='uncertain' if event == provider_failures.UNCERTAIN else 'unsafe', next_at=None)
        control['event_id'] = retry_event(state, notice, control, event, kind=args['kind'])
    elif action == 'reset':
        if control['status'] == 'recovered':
            return notice
        control.update(status='recovered', attempts=0, turn_id=args['turn_id'], next_at=None,
                       reservation_id=None)
    else:
        raise ValueError('Unknown provider retry action')
    notice['provider_retry'] = message_store.retry_record(control)
    state.publish(notice, 'status')
    return notice


class RetryMonitor:
    def __init__(self, state, policy, clock=time.time):
        self.state, self.policy, self.clock = state, policy, clock
        self.owners = {}

    def action(self, notice, action, **fields):
        return self.state.action(dict(op='provider-retry', id=notice['id'], action=action,
                                      at=self.clock(), **fields))

    def stop(self, notice, event, kind):
        return self.action(notice, 'stop', event=event, kind=kind)

    async def check_route(self, route):
        self.owners = {mid: owner for mid, owner in self.owners.items() if mid in self.state.messages}
        notice = retry_notice(self.state, route)
        if notice and notice['provider_retry']['status'] == 'reserved':
            self.stop(notice, provider_failures.UNCERTAIN, 'broker_restarted_during_reservation')
            return
        observation = await asyncio.to_thread(read_codex_observation, route['session'])
        snapshot, owner = observation['snapshot'], observation['owner']
        turn = latest_turn(snapshot)
        if (notice and isinstance(turn, dict) and turn.get('turnId')
                and turn['turnId'] != json.loads(notice['body'])['turn_id']
                and turn.get('status') in ('completed', 'interrupted')):
            self.action(notice, 'reset', turn_id=turn['turnId'])
            return
        failure = terminal_failure(snapshot)
        if failure is None:
            if notice and notice['provider_retry']['status'] == 'waiting':
                self.stop(notice, provider_failures.UNSAFE, 'native_retry_or_changed_turn')
            return
        source = last_sender_message(self.state, route)
        detected = self.state.action(dict(op='provider-failure', expected_route=route,
            source_message_id=source['id'] if source else None, failure=failure))
        if notice is None or notice['provider_retry']['status'] == 'recovered':
            notice = self.action(detected, 'arm', fingerprint=snapshot_fingerprint(snapshot),
                                 backoff=list(self.policy.backoff), cap=self.policy.cap)
        control = notice['provider_retry']
        if self.state.correspondence is not None and self.state.correspondence.loss:
            self.stop(notice, provider_failures.UNCERTAIN, 'correspondence_checkpoint_lost')
            return
        if control['status'] == 'accepted':
            if failure['turn_id'] != control['turn_id']:
                self.stop(notice, provider_failures.UNSAFE, 'newer_turn')
                return
            notice = self.action(notice, 'wait', failure=failure, fingerprint=snapshot_fingerprint(snapshot))
            control = notice['provider_retry']
        if control['status'] != 'waiting':
            return
        pinned = self.owners.setdefault(notice['id'], owner)
        refusal = retry_refusal(snapshot, control['turn_id'], control['fingerprint'])
        if pinned != owner or refusal:
            self.stop(notice, provider_failures.UNSAFE, refusal or 'desktop_owner_changed')
            return
        if self.clock() < control['next_at']:
            return
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()
        def reserve():
            if cancelled.is_set():
                raise RetryRefused('continuation_cancelled')
            future = concurrent.futures.Future()
            def commit():
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    if cancelled.is_set():
                        raise RetryRefused('continuation_cancelled')
                    future.set_result(self.action(notice, 'reserve', fingerprint=control['fingerprint']))
                except BaseException as error:
                    future.set_exception(error)
            loop.call_soon_threadsafe(commit)
            try:
                reservation = future.result(timeout=5)
                if cancelled.is_set():
                    raise RetryRefused('continuation_cancelled')
                return reservation
            except TimeoutError:
                future.cancel()
                raise
        try:
            accepted = await asyncio.to_thread(resume_failed_turn, route['session'], pinned,
                                               control['turn_id'], control['fingerprint'], reserve)
            current = self.state.get(notice['id'])
            self.action(current, 'finish', reservation_id=current['provider_retry']['reservation_id'],
                        outcome='accepted', turn_id=accepted)
        except RetryRefused as error:
            self.stop(self.state.get(notice['id']), provider_failures.UNSAFE, str(error))
        except codex_live.DeliveryError as error:
            event = provider_failures.UNCERTAIN if error.uncertain else provider_failures.UNSAFE
            self.stop(self.state.get(notice['id']), event,
                      'delivery_outcome_unknown' if error.uncertain else 'continuation_prewrite_failed')
        except asyncio.CancelledError:
            cancelled.set()
            current = self.state.get(notice['id'])
            if current['provider_retry']['status'] == 'reserved':
                self.stop(current, provider_failures.UNCERTAIN, 'continuation_cancelled')
            raise


async def observe_codex_failures(state, interval, policy=None):
    """Read each registered Codex thread and notify its last retained sender."""
    monitor = RetryMonitor(state, policy) if policy is not None else None
    while True:
        await asyncio.sleep(interval)
        for route in list(state.endpoints.values()):
            if route['transport'] != 'codex':
                continue
            if monitor is not None:
                try:
                    await monitor.check_route(route)
                except (ValueError, OSError, TypeError, KeyError, AttributeError):
                    continue
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
