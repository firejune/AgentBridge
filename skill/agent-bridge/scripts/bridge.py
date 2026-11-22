#!/usr/bin/env python3
"""Live agent correspondence over a local Unix socket. No database."""
import argparse
import asyncio
import errno
import hashlib
import json
import math
from pathlib import Path
import signal
import subprocess
import time
import sys
import os
import uuid

# Also resolve sibling modules when this entrypoint is imported via its root link.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_live
import broker
import codex_archive
import codex_live
import codex_preview  # Read-only Codex state helpers.
import codex_new
import codex_queue
# Keep the existing CLI imports of the shared delivery helpers available.
from delivery import (
    ARCHIVED_REASON, BEHIND_GRACE, BUSY_CLAIM, DELIVERED, FRESH_SESSION,
    PEER_BROKER_TIMEOUT, PEER_CLAIM_RELEASE_TIMEOUT, PEER_CLOSE_TAG,
    PEER_FROM_CHARS, PEER_IO_TIMEOUT, PEER_MAX_LINE, PEER_PUSH_TIMEOUT,
    PEER_TAG, REPLY_CLI, TITLE_WAIT, ClaimBusy, ClaudeDispatchUnknown, PushFailed,
    claim_behind, claim_peer, envelope, fresh_session, label_claude,
    legacy_claim_confirmed, peer_frame, peer_text, peer_token, public_wake, push,
    push_agy, push_codex, push_frame, push_if_peer, reply_command,
)
import models
from new_session_policy import CodexDesktopWakePolicyError
import sessions
import service
import usage
import session_usage
import wt
import retros
import receipts


def registered_route(args):
    route = next((r for r in broker.request(args.socket, {'op':'routes','project':args.project})
                  if r['name'] == args.name), None)
    if route is None:
        raise ValueError(f'No participant {args.name!r} is registered in project {args.project!r}')
    return route


def build_skew(path, ping=None):
    """Name a CLI and broker running different builds; None when equal or unanswered."""
    try:
        if ping is None:
            ping = broker.request(path, {'op': 'ping'}, deadline=time.monotonic() + 1)
        mine, theirs = broker.build_id(), ping.get('build')
    except (ValueError, OSError, AttributeError):
        return None
    if theirs == mine:
        return None
    found = f'is build {theirs}' if isinstance(theirs, str) else 'predates build reporting'
    return (f'BROKER_BUILD_SKEW: the running broker {found} and this CLI is build {mine}. '
            'Bring both to the same build before trusting a failure.')


def with_skew(args, result, error):
    """An operation the broker does not know is the mismatch itself: say so beside it.

    Only that definite refusal earns the extra read; no other failure touches the broker again.
    """
    warning = build_skew(args.socket) if str(error) == 'Unknown operation' else None
    return result if warning is None else {**result, 'warning': warning}


def receive_codex(args):
    """Read one retained peer message in the Codex thread it targets."""
    invalid = 'Run receive-codex from the addressed Codex thread with an exact message UUID'
    caller = os.environ.get('CODEX_THREAD_ID')
    try:
        if not caller or str(uuid.UUID(args.id)) != args.id:
            raise ValueError(invalid)
        thread_id = str(uuid.UUID(caller))
    except ValueError:
        raise ValueError(invalid) from None

    unmatched = 'No correspondence matches this Codex thread and message ID.'
    bad_response = 'Agent Bridge broker did not return a usable message response.'
    try:
        message = broker.request(args.socket, {'op': 'show', 'id': args.id})
    except (broker.BrokerConnectError, OSError) as error:
        if error.errno in (errno.EACCES, errno.EPERM):
            raise ValueError('Agent Bridge broker socket access was denied. '
                             'Check shell sandbox and socket permissions.') from None
        if isinstance(error, broker.BrokerConnectError):
            raise ValueError('Agent Bridge broker is unavailable for this socket.') from None
        raise ValueError(bad_response) from None
    except (broker.BrokerResponseError, KeyError, TypeError):
        raise ValueError(bad_response) from None
    except ValueError as error:
        # The existing wire response has no machine-readable error code.
        if str(error) == broker.UNKNOWN_MESSAGE_ERROR:
            raise ValueError(unmatched) from None
        raise ValueError(bad_response) from None
    if not isinstance(message, dict) or not isinstance(message.get('route'), dict):
        raise ValueError(bad_response)
    route = message['route']
    if message.get('id') != args.id or route.get('transport') != 'codex' or route.get('session') != thread_id:
        raise ValueError(unmatched)
    if 'close_request' in message:
        raise ValueError('The addressed message is a close handoff; claim it with close-operator.')
    if message.get('reason') == ARCHIVED_REASON:
        raise ValueError('The addressed correspondence failed because this thread is archived.')
    if message.get('body_expired'):
        raise ValueError('The addressed correspondence body has expired; only its record remains.')
    state = message.get('state')
    if state == 'pending':
        raise ValueError('The addressed correspondence has not been dispatched yet.')
    if state in broker.TERMINAL_STATES:
        raise ValueError(f'The addressed correspondence is already {state}; nothing is left to receive.')
    if state not in ('dispatching', 'submitted', 'uncertain', 'received', 'in_progress'):
        raise ValueError(bad_response)  # Not a state this CLI knows; never echo it.
    if not isinstance(message.get('body'), str) or not isinstance(message.get('sender'), str):
        raise ValueError(bad_response)
    try:
        # The same text direct delivery hands the thread: envelope, body and reply contract.
        text = peer_text(message, args.socket, escape_peer=False)
    except (KeyError, TypeError):
        raise ValueError(bad_response) from None
    # A message already received or in progress was read before; acking again would add an event
    # (or move in_progress back) for nothing (issue #153).
    marked = state not in ('received', 'in_progress') and mark_received(args.socket, args.id)
    return {'id': args.id, 'sender': message['sender'], 'text': text,
            'provenance': 'agent_bridge_peer', 'authorization': 'none', 'marked_received': marked}


def mark_received(path, mid):
    """Record that the addressed Codex thread retrieved the message: `submitted` becomes `received`.

    Best-effort and bounded. The broker refuses while delivery is unresolved (`dispatching`,
    `uncertain`), once the message is complete, or while the route is closing; any refusal or
    broker error leaves the read exactly as it was and reports False.
    """
    try:
        marked = broker.request(path, {'op': 'ack', 'id': mid}, deadline=time.monotonic() + 5)
    except Exception:  # The read already succeeded; nothing about the marker may fail it.
        return False
    return isinstance(marked, dict) and marked.get('state') == 'received'


def wake_participant(args):
    """bridge.py wake NAME --project P: bring an idle-closed claude-peer session back (issue #115)."""
    route = registered_route(args)
    base = {'name':args.name, 'project':args.project, 'transport':route['transport'], 'session':route['session']}
    if route['transport'] != 'claude-peer':
        return {**base, 'state':'unsupported', 'host':None,
                'reason':f"{route['transport']}: wake covers claude-peer routes only"}
    return {**base, **public_wake(sessions.wake_claude(route['session'], timeout=args.timeout))}


class ModelRelay:
    """The message path models.claude_desktop_switch asks a command post through."""
    def __init__(self, path):
        self.path = path

    def routes(self, project):
        return broker.request(self.path, {'op':'routes','project':project})

    def ask(self, project, recipient, cwd, body, key, timeout):
        # The sender is a realtime route so the command post's reply is held for
        # this wait and never pushed into the session being switched.
        mine = next((r for r in self.routes(project) if r['name'] == models.RELAY_NAME), None)
        if mine is not None and mine['transport'] != 'realtime':
            raise ValueError(f'Participant {models.RELAY_NAME!r} in project {project!r} is a '
                             f"{mine['transport']} route; the model relay needs that name for a realtime sender")
        if mine is None:
            broker.request(self.path, {'op':'register','project':project,'name':models.RELAY_NAME,
                                       'transport':'realtime','cwd':cwd})
        sent = push_if_peer(self.path, broker.request(self.path, {'op':'send','project':project,
                            'sender':models.RELAY_NAME,'recipient':recipient,'body':body,'key':key}))
        waited = broker.request(self.path, {'op':'wait','id':sent['id'],'timeout':timeout}, timeout=timeout+5)
        return sent, waited


def command(m):
    route = m['route']
    payload = envelope(m)
    if route['transport'] == 'codex':
        raise ValueError('Codex uses the existing desktop IPC owner, not a queued CLI process')
    if route['transport'] == 'agy':
        raise ValueError('Antigravity uses the live desktop server, not a queued CLI process')
    if route['transport'] == 'claude':
        return [*sessions.cli_prefix(route['executable'] or 'claude'), '--resume', route['session'], '--model', 'opus',
                '--strict-mcp-config', '--output-format', 'json', '--permission-prompts', 'none',
                '--permission-mode', 'plan', '--tools', 'Read,Grep,Glob', '-p', payload]
    raise ValueError('Realtime route: subscribe with watch; no external CLI dispatch needed')



def deliver(path, mid, timeout=180, dry_run=False):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Timeout must be a finite positive number')
    deadline = time.monotonic() + timeout

    def rpc(op, **fields):
        return broker.request(path, {"op":op, "id":mid, **fields})

    def finish(state, note=None):
        return rpc("finish", state=state, note=note)
    m = broker.request(path, {'op':'show','id':mid}, deadline=min(deadline, time.monotonic() + 10))
    if m['route']['transport'] == 'codex':
        return push_codex(path, m, timeout, dry_run)
    if m['route']['transport'] == 'claude-peer':
        if m['state'] != 'pending' and not dry_run:
            raise ValueError('Only pending messages may be delivered')
        return push(path, m, dry_run, deadline=deadline)
    if m['route']['transport'] == 'agy':
        if m['state'] != 'pending' and not dry_run:
            raise ValueError('Only pending messages may be delivered')
        return push_agy(path, m, timeout, dry_run)
    cmd = command(m)
    route = m['route']
    if dry_run:
        return {'sent': False, 'state': m['state'], 'cwd': route['cwd'], 'argv': cmd}
    if not Path(route['cwd']).is_dir():
        raise ValueError('Registered working directory is missing')
    rpc("claim")
    process = None
    try:
        process = subprocess.Popen(cmd, cwd=route['cwd'], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        stdout, stderr = process.communicate(timeout=timeout)
        if process.returncode != 0:
            finish('uncertain', stderr[-4000:] or stdout[-4000:])
        elif route['transport'] == 'claude':
            result = sessions.cli_json(stdout)
            if not isinstance(result, dict) or result.get('is_error') or result.get('permission_denials') or not isinstance(result.get('result'), str) or not result['result'].strip():
                raise ValueError('Claude reported an error, permission denial, or no text result')
            # Store the response atomically with the parent's answered state.
            rpc('reply', body=result['result'], key='cli-response:' + mid)
        else:
            finish('submitted', stdout[-4000:])
    except OSError as error:
        finish('failed' if process is None else 'uncertain', str(error))
    except (subprocess.TimeoutExpired, KeyboardInterrupt, ValueError) as error:
        if process is not None and (process.poll() is None or isinstance(error, subprocess.TimeoutExpired)):
            # The leader may exit while a child still holds stdout/stderr open.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
        finish('uncertain', str(error))
    return rpc("show")



def output(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)


class RegistrationConflict(ValueError):
    """A known occupied name, before any external creation."""

    def __init__(self, route):
        super().__init__(broker.REGISTER_CONFLICT_ERROR)
        self.route = {key: route.get(key) for key in ('transport', 'session', 'cwd')}


def require_free_registration_name(path, project, name):
    broker.identifier(project)
    broker.identifier(name)
    rows = broker.request(path, {'op': 'routes', 'project': project})
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Registration preflight returned invalid routes')
    holder = next((row for row in rows if row.get('name') == name), None)
    if holder is not None:
        raise RegistrationConflict(holder)


def validate_new_registration(args):
    if args.discover or args.session:
        raise ValueError('--new cannot be combined with --session or --discover')
    if args.transport not in ('agy','claude-peer','codex'):
        raise ValueError('--new is supported only with --transport agy (Antigravity desktop or IDE), claude-peer (Claude Code desktop or IDE), or codex (Codex desktop)')
    if not args.cwd:
        raise ValueError('--new requires --cwd')


def registration(args):
    observation = None
    if getattr(args, 'new', False):
        validate_new_registration(args)
        provider = 'claude' if args.transport == 'claude-peer' else args.transport
        title = args.title
        if args.transport == 'agy' and (not isinstance(title, str) or not title.strip()):
            title = getattr(args, 'name', None)
        observation = sessions.open_session(provider, cwd=args.cwd, title=title, model=args.model, timeout=args.timeout,
                                            host=getattr(args, 'host', 'desktop'), ide=getattr(args, 'ide', None))
        args.session = observation['session']['id']
        args.cwd = observation['session']['cwd']
        return observation
    if args.discover:
        if args.transport == 'realtime' or not args.session:
            raise ValueError('--discover requires a CLI transport and exact --session UUID')
        if args.transport == 'claude-peer':
            observation = {'session':sessions.resolve_claude_peer(args.session),'warning':None}
        else:
            observation = sessions.inspect_session(args.transport,args.session,args.executable,args.codex_mode)
        cwd = observation['session']['cwd']
        if args.cwd and Path(args.cwd).expanduser().resolve() != Path(cwd).resolve():
            raise ValueError('Requested cwd differs from the discovered session cwd')
        args.cwd = cwd
    if not args.cwd:
        raise ValueError('Provide --cwd or use --discover to infer it')
    return observation


def broker_state_dir(args):
    return Path(getattr(args, 'state_dir', None) or Path(args.socket).expanduser().absolute().with_suffix('.state'))


def new_session_policy(args, provider):
    return models.new_session_policy(args, provider, broker_state_dir(args))


def open_codex_result(args):
    """Keep Codex creation, refusal and route recovery in one result envelope."""
    result = codex_new.open_result()
    source, cancelled = None, False
    default_title = None
    step = 'input'
    try:
        if args.cmd == 'open':
            if (args.register is None) != (args.project is None):
                raise codex_new.CreationError('--register NAME and --project P must be supplied together')
        elif args.session or args.discover:
            raise codex_new.CreationError('--new cannot be combined with --session or --discover')
        name = args.register if args.cmd == 'open' else args.name
        if name is not None:
            for label, value in (('name', name), ('project', args.project)):
                try:
                    broker.identifier(value)
                except ValueError:
                    raise codex_new.CreationError(f'Codex route {label} must be a lowercase identifier') from None
        step = 'policy'
        source = new_session_policy(args, 'codex')
        step = 'routes'
        if name is not None:
            require_free_registration_name(args.socket, args.project, name)
        step = 'open'
        title = args.title
        if title is None and name:
            title = default_title = f'{args.project}/{name}'
        result = sessions.open_session('codex', cwd=args.cwd, title=title, model=args.model,
            timeout=args.timeout, host=args.host, ide=args.ide, force_new=args.new, allow_queue_wake=True,
            queue_wake_workspace_write=getattr(args, 'queue_wake_workspace_write', False))
    except RegistrationConflict as error:
        result.update(error=str(error), route_conflict=error.route)
    except broker.BrokerConnectError:
        result['error'] = 'Agent Bridge broker is unavailable for this socket; no Codex thread was created'
    except codex_new.CreationError as error:
        result = error.result
        if default_title is not None and result['stage'] == 'duplicate':
            result['error'] = result['error'].replace('match title ', 'match default title ', 1)
        cancelled = error.cancelled
    except KeyboardInterrupt:
        result['error'] = 'Codex open interrupted before a result was available'
        cancelled = True
    except sessions.DiscoveryError as error:
        result['error'] = str(error)  # A fixed --host/--ide refusal, before any provider call.
    except CodexDesktopWakePolicyError as error:
        result['error'] = str(error)  # Fixed text, with no policy path or contents.
    except (ValueError, OSError, subprocess.TimeoutExpired, KeyError, TypeError):
        # Policy files and transport exceptions can contain local paths or secrets: name the step, not the text.
        result['error'] = {
            'policy': 'Codex open stopped: the model policy could not be read; '
                      'check policy.json in the broker state directory',
            'routes': 'Codex open stopped: the broker did not return usable routes for this project',
        }.get(step, 'Codex open preflight failed; check provider, host and model policy')
    if source is not None:
        result['model_source'] = source
    name = args.register if args.cmd == 'open' else args.name
    can_register = (not cancelled and result['created'] and result['adopted']
                    and result['error'] is None and result['stage'] == 'complete'
                    and result['session']['id'] and result['session']['cwd'])
    if name and can_register:
        try:
            broker.request(args.socket, {'op': 'register', 'name': name, 'project': args.project,
                'transport': 'codex', 'session': result['session']['id'], 'cwd': result['session']['cwd']})
            result['registered'] = True
        except (ValueError, OSError, subprocess.TimeoutExpired, KeyError, TypeError) as error:
            # Exact authoritative refusal is definite; other broker write errors
            # remain uncertain and are never retried automatically.
            conflict = isinstance(error, ValueError) and str(error) == broker.REGISTER_CONFLICT_ERROR
            result['registration_error'] = (broker.REGISTER_CONFLICT_ERROR if conflict else
                'Codex route registration could not be confirmed; inspect routes before retrying')
            result['uncertain'] = not conflict
            if result['error'] is None:
                result.update(error=result['registration_error'], stage='registration')
        except KeyboardInterrupt:
            result['registration_error'] = 'Codex route registration interrupted; inspect routes before retrying'
            result['uncertain'] = True
            cancelled = True
            if result['error'] is None:
                result.update(error=result['registration_error'], stage='registration')
    if result['error'] and result['created'] and result['session']['id'] and not result['registered']:
        # A thread exists that no route points at. Name the one way back to it.
        sid = result['session']['id']
        result['recovery'] = (
            f'Codex thread {sid} exists and is not registered. Never repeat open or register --new for it. '
            'Inspect that thread in Codex Desktop, then bind it with: '
            f"register {name or 'NAME'} --project {args.project or 'PROJECT'} "
            f'--transport codex --session {sid} --discover')
    permissions = result.get('permission_settings') or {}
    approval = permissions.get('approvalPolicy')
    sandbox = (permissions.get('sandboxPolicy') or {}).get('type')
    status = 'failed' if result['error'] else 'confirmed'
    print(f"Codex open {status}: stage={result['stage']} approvalPolicy={approval} sandbox={sandbox}",
          file=sys.stderr, flush=True)
    output(result)
    return 130 if cancelled else (1 if result['error'] else 0)


def close_session(route, timeout, archive=False, *, directory=None, prefer_native=None):
    """Stop or detach one route's session; optionally archive it out of the sidebar."""
    transport, sid = route['transport'], route.get('session')
    if transport == 'claude-peer':
        try:
            return sessions.close_claude(
                sid,
                own_session=os.environ.get('CLAUDE_CODE_SESSION_ID'),
                timeout=timeout,
                archive=archive,
            )
        except sessions.DiscoveryError as error:
            if str(error).startswith('unsupported host '):
                host = str(error).removeprefix('unsupported host ')
                raise models.Unsupported(f'claude ({host}): closing is measured only on the IDE extension '
                                         'and desktop; close this session in its app') from None
            raise
    if transport == 'agy':
        if archive:
            return agy_live.archive_agy(sid, timeout=timeout)
        return agy_live.close_agy(sid, timeout=timeout)
    if transport == 'codex':
        if archive:
            return codex_archive.close(route, directory, timeout, prefer_native=prefer_native)
        return {
            'closed': True,
            'method': 'detach',
            'archived': False,
            'session': sid,
            'host': 'codex',
            'resumable': True,
            'note': 'The transcript is kept in the Codex sidebar; sending a message in that thread resumes the same session.',
        }
    return {'closed': False, 'method': 'none', 'archived': False, 'session': sid,
            'note': f'{transport} routes have no provider session to close'}


def prepare_retro(args, route=None):
    """Read and validate a retrospective; nothing is written or sent until record_retro."""
    state = broker_state_dir(args)
    path = retros.store_path(state, args.project, args.store)
    text_file = args.retro_file if route is not None else args.from_file
    text = None
    if text_file:
        with Path(text_file).open(encoding='utf-8', newline='') as source:
            text = source.read()
    reason = args.no_retro if route is not None else None
    # Validate the requirement before any provider read, write or notification.
    row = retros.record(args.project, args.card, route['name'] if route else args.runner,
                        None if route else args.model, route.get('session') if route else args.session,
                        text=text, reason=reason, tower=args.tower, urgency=args.urgency, tags=args.tags)
    target = retros.notify_route(state, args.project, args.notify)
    if target and row['urgency'] in ('stop', 'repeat'):
        routes = broker.request(args.socket, {'op':'routes', 'project':args.project})
        names = {r['name'] for r in routes}
        if target not in names or row['runner'] not in names:
            raise ValueError('RETRO_NOTIFY_ROUTE_MISSING: notification needs registered runner and target routes')
    if route is not None and text is not None:
        reading = models.read(route, min(args.timeout, 10))
        row['model'] = reading.get('model')
        # Provider diagnostics never belong in the retro ledger.
    return path, row, target


def record_retro(args, prepared):
    path, row, target = prepared
    retros.append(path, row)
    result = {'retro_id':row['id'], 'store':str(path), 'notification':None}
    if target and row['urgency'] in ('stop', 'repeat'):
        excerpt = '\n'.join((row['text'] or row['no_retro']).splitlines()[:5])[:1000]
        body = (f"Retro {row['urgency']}: card={row['card']} runner={row['runner']} "
                f"tags={','.join(row['tags'])}\n{excerpt}")
        try:
            sent = broker.request(args.socket, {'op':'send', 'project':args.project,
                       'sender':row['runner'], 'recipient':target, 'body':body, 'key':'retro-'+row['id']})
            result['notification'] = {'id':sent['id'], 'state':sent['state']}
            sent = push_if_peer(args.socket, sent)
            result['notification']['state'] = sent['state']
        except (ValueError, OSError) as error:
            # Saved already: expose the receipt and never retry a possibly dispatched message.
            result['notification'] = {**(result['notification'] or {}), 'error':str(error), 'retry':False}
    return result


def record_closing_retro(args, prepared, result):
    """Write a close's retrospective once the close is confirmed; a failed close writes none (issue #144)."""
    if prepared is None:
        return
    try:
        result.update(record_retro(args, prepared))
    except (ValueError, OSError) as error:
        # The session is already closed. Name the gap; `retro add` can still record it.
        result.update(retro_recorded=False, retro_error=str(error))


def retro_command(args):
    if args.retro_cmd == 'add':
        return record_retro(args, prepare_retro(args))
    found = retros.rows(retros.store_path(broker_state_dir(args), args.project, args.store),
                        args.project, args.since, getattr(args, 'tag', None), getattr(args, 'urgency', None))
    return retros.tally(found) if args.retro_cmd == 'tally' else found


def close_participant(args):
    if not 0 < args.timeout <= 600:
        raise ValueError('Timeout must be between 0 and 600 seconds')
    address = {'name': args.name, 'project': args.project}
    if args.saved_state:
        raise ValueError(broker.SAVED_STATE_REMOVED)
    if args.finish:
        if not args.expect_session:
            raise ValueError('--finish requires --expect-session')
        if args.keep_route:
            raise ValueError('--finish removes the verified route and cannot be combined with --keep-route')
        if args.handoff:
            raise ValueError('--finish completes a prepared handoff and cannot be combined with --handoff')
        if args.retro_file or args.no_retro:
            raise ValueError('Record the retrospective when preparing the archive handoff')
        request = {'op': 'close-finish', **address, 'close_id': str(uuid.UUID(args.finish)),
                   'session': str(uuid.UUID(args.expect_session)), 'timeout': args.timeout}
        closed = broker.request(args.socket, request, timeout=args.timeout + 5)
        result = {**address, 'transport': 'codex', 'method': 'archive', 'archived': True, 'resumable': True,
                  'status': 'closed' if closed['route_removed'] else 'archive_verified',
                  'closed': True, 'session': closed['session'], 'close_id': closed['closing']}
        result.update({key: closed[key] for key in ('route_removed', 'correspondence_cleanup',
                       'cleanup_error', 'route_error', 'operator_receipt', 'operator_error') if key in closed})
        return result
    route = registered_route(args)
    if args.expect_session and str(uuid.UUID(args.expect_session)) != route['session']:
        raise ValueError('Endpoint does not match --expect-session')
    archive = args.archive
    if args.handoff:
        if not archive:
            raise ValueError('--handoff requires --archive')
        if args.keep_route:
            raise ValueError('--handoff removes the verified route and cannot be combined with --keep-route')
        if route['transport'] != 'codex':
            raise ValueError('--handoff requires a Codex route')
    if 'closing' in route and not args.handoff:
        if archive and not args.keep_route and 'closing_native_scope' in route:
            pending = prepare_archive_handoff(args, address, route)
            return {**pending, 'archive_incomplete': not pending['closed']}
        raise ValueError('Codex route is closing; finish the native archive handoff')
    if archive and 'closing' not in route:
        try:
            broker.request(args.socket, {'op':'archive-correspondence', 'expected_route':route, 'check_only':True})
        except ValueError as error:
            if str(error) == 'Unknown operation':
                raise ValueError('Archive cleanup is unsupported by the running broker; upgrade it before archiving') from None
            raise
    # Validated and read while the session is still open; written only after the close is confirmed.
    retro = None if 'closing' in route else prepare_retro(args, route)
    if args.handoff:
        # The handoff marks the route closing, and a closing route cannot send the stop/repeat
        # notification. So this one path records first; the native archive is confirmed later by --finish.
        if 'closing' not in route:
            # Check the operator before the route is reserved; a dead operator strands it (#196).
            preflight_native_close_operator(args, route, explicit=True)
        receipt = {} if retro is None else record_retro(args, retro)
        return {**receipt, **prepare_archive_handoff(args, address, route)}
    directory = broker_state_dir(args).expanduser().resolve()
    operator = None

    def prefer_native():
        nonlocal operator
        operator = preflight_native_close_operator(args, route)
        return operator is not None

    preflight = prefer_native if archive and not args.keep_route else None
    result = {**address, 'transport': route['transport'],
              **close_session(route, args.timeout, archive=archive, directory=directory, prefer_native=preflight)}
    if result.get('status') == 'awaiting_native_archive':
        scope = result.pop('native_scope')
        try:
            prepare_archive_handoff(args, address, route, expected_operator=operator, native_scope=scope,
                                    check_only=True)
            if retro is not None:
                result.update(record_retro(args, retro))
            result.update(prepare_archive_handoff(args, address, route, expected_operator=operator,
                                                  native_scope=scope))
        except (broker.BrokerResponseError, OSError):
            result.update(status='native_archive_preparation_unconfirmed', route_removed=False,
                          operator_error='Native archive handoff could not be confirmed; inspect the route')
        except ValueError as error:
            result.update(status='native_archive_preparation_refused', route_removed=False, operator_error=str(error))
        result['archive_incomplete'] = not result['closed']
        return result
    if archive and (result.get('archived') is not True or result.get('session') != route['session']):
        result.update(correspondence_removed=False, cleanup_error='Provider archival was not confirmed', route_removed=False)
        if retro is not None:
            result['retro_recorded'] = False
        return result
    # Before the route is removed: a stop/repeat retro is sent from the runner's route.
    record_closing_retro(args, retro, result)
    if archive:
        try:
            cleanup = broker.request(args.socket, {'op':'archive-correspondence', 'expected_route':route, 'archived':True})
            result['correspondence_cleanup'] = cleanup
            if not cleanup['complete'] and cleanup.get('active_retained') != 0:
                result['cleanup_error'] = 'Archive confirmed; protected correspondence remains (active or uncertain delivery/wait)'
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            # Provider archival already succeeded. Do not turn partial success into
            # a generic error or retry an ambiguously accepted purge.
            uncertain = isinstance(error, (OSError, subprocess.TimeoutExpired, broker.BrokerResponseError))
            result.update(cleanup_error=str(error), cleanup_uncertain=uncertain, route_removed=False)
            result['correspondence_cleanup'] = {'confirmed':False, 'state':'unknown' if uncertain else 'rejected'}
            return result
    if args.keep_route:
        result['route_removed'] = False
        return result
    try:
        request = {'op': 'unregister', **address}
        if archive:
            request.update(op='unregister-archived', expected_route=route, archived=True)
        elif route['transport'] == 'codex':
            request.update(op='unregister-if-current', expected_route=route)
        broker.request(args.socket, request)
        result['route_removed'] = True
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        if not archive and not isinstance(error, ValueError):
            raise  # Ordinary close preserves its existing I/O error behavior.
        result['route_removed'] = False
        if archive:
            result['route_error'] = str(error)
            result.setdefault('cleanup_error', 'Archive confirmed; route removal was not confirmed')
        result['route_note'] = (('The running broker lacks guarded archive route removal; upgrade it before removing the route'
                                 if archive else 'The running broker predates route removal; restart it to pick up this version')
                                if str(error) == 'Unknown operation' else str(error))
    return result


def preflight_native_close_operator(args, target, *, explicit=False):
    request = {'op': 'close-operator-status', 'expected_route': target}
    if explicit:
        request['explicit'] = True  # Any enrollment dispatches an explicit handoff.
    try:
        status = broker.request(args.socket, request)
    except ValueError as error:
        if str(error) == 'Unknown operation':
            raise ValueError('The running broker cannot verify the Codex close operator preference; upgrade it') from None
        raise
    if not isinstance(status, dict) or 'operator' not in status:
        raise ValueError('Could not verify the Codex close operator preference')
    operator = status['operator']
    if operator is not None:
        # Explicit handoffs dispatch over IPC only; direct ones may use the verified queue.
        allow_queue = not explicit and Path(args.socket).expanduser() == broker.DEFAULT_SOCKET
        try:
            codex_live.deliver(operator['route'], str(uuid.uuid4()), '', args.timeout, dry_run=True,
                               allow_queue=allow_queue, close_handoff=True)
        except codex_live.EndpointUnavailable as error:
            detail = ' The local Desktop queue could not be verified.' if allow_queue and error.default_absent else ''
            raise ValueError(f'Enrolled Codex close operator is unavailable. {error}{detail}') from None
        except codex_live.DeliveryError as error:
            raise ValueError(f'Enrolled Codex close operator is unavailable. {error}') from None
    return operator


def prepare_archive_handoff(args, address, route, *, expected_operator=None, native_scope=None, check_only=False):
    expected = {key: value for key, value in route.items() if key not in ('closing', 'closing_native_scope')}
    request = {'op': 'close-prepare', **address, 'expected_route': expected}
    if native_scope is not None:
        request.update(native_scope=native_scope, expected_operator=expected_operator)
    if check_only:
        request['check_only'] = True
    prepared = broker.request(args.socket, request)
    if check_only:
        if prepared != {'ready': True}:
            raise broker.BrokerResponseError('Broker did not confirm native archive preparation checks')
        return None
    target = prepared['route']
    result = {**address, 'status': 'awaiting_native_archive', 'closed': False, 'route_removed': False,
              'transport': 'codex', 'close_id': target['closing'],
              'session': target['session'], 'cwd': target['cwd']}
    message = prepared.get('message')
    if prepared.get('operator') is not None:
        try:
            dispatch = broker.request(args.socket, {'op': 'close-dispatch', **address,
                'session': result['session'], 'close_id': result['close_id'],
                'operator': prepared['operator']})
            message = dispatch['message']
            if dispatch['created']:
                message = push_codex(args.socket, message, args.timeout, close_dispatch=True,
                                     guarded_close='closing_native_scope' in target)
                if 'delivery_path' in message:
                    result['operator_delivery_path'] = message['delivery_path']
        except PushFailed as error:
            message = error.message
            result['operator_error'] = 'Codex close handoff delivery could not be confirmed'
        except (ValueError, OSError, KeyError, TypeError):
            result['operator_error'] = 'Codex close handoff could not be dispatched; inspect the closing route'
    if message is not None:
        result.update(operator_message_id=message['id'], operator_delivery=message['state'])
        if message['state'] in ('pending', 'uncertain', 'failed'):
            result.setdefault('operator_error', 'Codex close handoff has no confirmed delivery')
        elif message['state'] == 'done':
            result.update(status='closed', closed=True, archived=True, route_removed=True)
            result.pop('operator_error', None)
    elif 'closing_native_scope' in target:
        result.setdefault('operator_error', 'No dispatcher was confirmed for the native archive handoff')
    return result


USAGE_STATE_HELP = ('Broker state directory holding usage samples, when the broker runs with its own '
                    '--state-dir (default: socket path with .state suffix)')
COLLECTED_USAGE = {'codex': 'codex', 'agy': 'agy', 'claude-peer': 'claude'}  # transport -> provider the broker collects limits for


def message_usage(args, message):
    """Usage lines for a message that involves a collected provider (#32); None otherwise."""
    transports = {(message.get(k) or {}).get('transport') for k in ('route', 'return_route')}
    providers = sorted({COLLECTED_USAGE[t] for t in transports if t in COLLECTED_USAGE})
    if len(providers) > 1 and 'claude' in providers:
        providers.remove('claude')  # Claude lines only between Claude sessions (#83); others keep their shape.
    if not providers:
        return None
    briefs = [usage.brief(broker_state_dir(args), p) for p in providers]
    return briefs[0] if len(briefs) == 1 else briefs


def with_usage(args, message):
    found = message_usage(args, message)
    return message if found is None else {**message, 'usage': found}


def with_models(rows, policy, timeout, key='session'):
    """Annotate live rows with a fresh model reading; never cached as truth."""
    for row in rows:
        route = row if 'transport' in row else {'transport':{'claude':'claude-peer'}.get(row['provider'],row['provider']),
                                                 'session':row['id']}
        row['model'] = models.flag(policy, models.read(route, timeout))
    return rows


def add_broker_commands(sub):
    """The broker itself: serve, its retention and usage reads, ping, stop and the launchd service."""
    p = sub.add_parser('serve',help='Run the local broker in the foreground')
    p.add_argument('--max-messages',type=int,default=2048,help='Active-message cap; TTL mode refuses admission when no safely expired body exists')
    retention_mode = p.add_mutually_exclusive_group()
    retention_mode.add_argument('--retention-prepare',action='store_true',help='Warm up legacy timestamps without TTL deletion; current admission policy remains')
    retention_mode.add_argument('--retention-ttl',action='store_true',help='Opt in: remove submitted/received after 24h from creation; compact completed bodies with 7d lookup (preflight first)')
    p.add_argument('--state-dir',help='Private routing snapshot directory (default: socket path with .state suffix)')
    p.add_argument('--usage-interval',type=int,default=usage.DEFAULT_INTERVAL_MINUTES,
                   help=f'Minutes between limit reads (0 = off, minimum {usage.MIN_INTERVAL_MINUTES})')
    p.add_argument('--codex-executable',default='codex')
    p.add_argument('--codex-failure-interval',type=int,default=broker.codex_failures.DEFAULT_INTERVAL,
                   help='Seconds between read-only checks of registered Codex threads (0 = off)')
    p.add_argument('--agy-failure-interval',type=int,default=agy_live.DEFAULT_FAILURE_INTERVAL,
                   help='Seconds between read-only checks of registered Antigravity sessions (0 = off)')
    p.add_argument('--provider-retry-backoff',default=','.join(map(str,broker.provider_failures.DEFAULT_BACKOFF)),
                   help='Comma-separated positive delays before continuation attempts, in seconds')
    p.add_argument('--provider-retry-cap',type=int,default=broker.provider_failures.DEFAULT_CAP,
                   help='Maximum accepted continuation attempts in a failure episode')
    p.add_argument('--no-claude-live',dest='claude_live',action='store_false',
                   help='Read the Claude Code login from its ~/.claude.json cache only; never read its Keychain token')
    p = sub.add_parser('retention',help='Read-only counts-only retention preflight; never deletes or changes routing')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run',action='store_true',help='Read checkpoint only; live waiters are invisible, counts are upper bounds')
    g.add_argument('--live',action='store_true',help='Use running broker planner including active waits')
    p.add_argument('--state-dir')
    p.add_argument('--max-messages',type=int,default=2048)
    p = sub.add_parser('session-usage', help='Read one local session record; starts no turn')
    p.add_argument('name', nargs='?')
    p.add_argument('--project')
    p.add_argument('--provider', choices=['codex', 'claude', 'agy'])
    p.add_argument('--id')
    p.add_argument('--prices', metavar='FILE')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('usage',help='Recorded subscription limits; reads the history file, starts nothing')
    p.add_argument('--provider',choices=['all', *usage.PROVIDERS],default='all')
    p.add_argument('--state-dir',help='Broker state directory (default: socket path with .state suffix)')
    p.add_argument('--since',default='6h',help='Burn-rate and reset window, e.g. 90m, 6h, 2d')
    p.add_argument('--json',action='store_true',help='Full records and summary')
    p.add_argument('--events',action='store_true',help='Reset events, early-reset intervals, the next-reset estimate and the spend/stop-risk horizons')
    p.add_argument('--refresh',action='store_true',help='Force a fresh Antigravity quota read before querying')
    p.add_argument('--seed-announcements',metavar='FILE',help='Merge promo-reset announcements (JSONL: at with UTC offset, note) into the state dir, then exit')
    sub.add_parser('ping')
    sub.add_parser('stop')
    for cmd in ('install-service','uninstall-service'):
        p = sub.add_parser(cmd,help='Opt-in per-user launchd service management')
        p.add_argument('--state-dir')
        p.add_argument('--plist',help='Plist path (default: ~/Library/LaunchAgents/com.agent-bridge.broker.SOCKET_HASH.plist)')
        p.add_argument('--generate-only',action='store_true',help='Write/remove the plist only; never call launchctl')
        if cmd == 'install-service':
            p.add_argument('--max-messages',type=int,default=2048)
            mode = p.add_mutually_exclusive_group()
            mode.add_argument('--retention-prepare',action='store_true',help='Generate service arguments for non-deleting timestamp warm-up')
            mode.add_argument('--retention-ttl',action='store_true',help='Generate service arguments for opt-in TTL cleanup')


def add_session_commands(sub):
    """Routes and the sessions behind them: register, open, model, close."""
    p = sub.add_parser('register',help='Bind an unused project/name or retry its exact existing identity')
    p.add_argument('name')
    p.add_argument('--project',required=True)
    p.add_argument('--transport',choices=['realtime','codex','claude','claude-peer','agy'],default='realtime')
    p.add_argument('--session')
    p.add_argument('--cwd')
    p.add_argument('--executable')
    p.add_argument('--discover',action='store_true')
    p.add_argument('--new',action='store_true',help='Open a new Antigravity, Claude Code, or Codex session and register it')
    p.add_argument('--title')
    p.add_argument('--model', help=f'New-session model (Codex: flag > AGENT_BRIDGE_CODEX_MODEL > policy defaults.codex > {codex_new.DEFAULT_MODEL})')
    p.add_argument('--timeout',type=float,default=180,help='Seconds to wait for desktop session creation')
    p.add_argument('--host',choices=['desktop','ide'],default='desktop',help='Host for --new (claude-peer, agy): desktop app or IDE. Claude desktop needs Enter; Claude IDE, agy desktop and agy IDE do not')
    p.add_argument('--ide',choices=sorted(sessions.CLAUDE_IDES),help=f'IDE for --host ide (default {sessions.DEFAULT_IDE})')
    p.add_argument('--codex-mode',choices=['auto','shared','saved'],default='auto')
    p.add_argument('--state-dir',help='Broker state directory holding policy.json (default: socket path with .state suffix)')
    p = sub.add_parser('open',help='Open a new live session (Claude Code, Codex, Antigravity; desktop or IDE)')
    p.add_argument('--provider',choices=['agy','claude','codex'],default='agy')
    p.add_argument('--cwd',required=True)
    p.add_argument('--title')
    p.add_argument('--model', help=f'New-session model (Codex: flag > AGENT_BRIDGE_CODEX_MODEL > policy defaults.codex > {codex_new.DEFAULT_MODEL})')
    p.add_argument('--timeout',type=float,default=180)
    p.add_argument('--host',choices=['desktop','ide'],default='desktop',help='Host (claude, agy): desktop app or IDE. Claude desktop needs Enter; Claude IDE, agy desktop and agy IDE do not')
    p.add_argument('--ide',choices=sorted(sessions.CLAUDE_IDES),help=f'IDE for --host ide (default {sessions.DEFAULT_IDE})')
    p.add_argument('--state-dir')
    p.add_argument('--register', metavar='NAME', help='Codex: bind a verified new thread to an unused project/name')
    p.add_argument('--project', help='Project for --register')
    p.add_argument('--new', action='store_true', help='Codex: explicitly create despite a saved title/cwd match')
    p = sub.add_parser('model',help="Read a participant's model, or switch it with --set (provider-neutral)")
    p.add_argument('name')
    p.add_argument('--project',required=True)
    p.add_argument('--set',dest='target')
    p.add_argument('--timeout',type=float,default=30)
    p.add_argument('--state-dir')
    p = sub.add_parser('archive-sweep', help='Review or apply a guarded Codex archive manifest')
    p.add_argument('--cwd-prefix', help='Absolute workspace prefix, matched by path components')
    p.add_argument('--apply', action='store_true', help='Apply a reviewed manifest')
    p.add_argument('--manifest', type=Path, help='Reviewed JSON manifest (required with --apply)')
    p.add_argument('--state-dir', help='Broker routing directory (defaults to the socket .state directory)')
    p.add_argument('--timeout', type=float, default=10, help='Seconds per Codex RPC')
    p = sub.add_parser('close',help="Close a participant's session without deleting it, then remove its route")
    p.add_argument('name')
    p.add_argument('--project',required=True)
    archive = p.add_mutually_exclusive_group()
    archive.add_argument('--archive',action='store_true',
                         help='Archive the session and clean its unprotected broker correspondence')
    archive.add_argument('--no-archive',dest='archive',action='store_false',default=False,
                         help='Close the session without archiving (the default)')
    archive.add_argument('--finish',metavar='CLOSE_ID',help='Verify the native Codex archive and remove the prepared route')
    p.add_argument('--handoff',action='store_true',help='Prepare a native Codex archive handoff with --archive')
    p.add_argument('--expect-session',metavar='UUID',help='Match the exact session (required with --finish)')
    p.add_argument('--saved-state',action='store_true',
                   help='Removed (#212): Agent Bridge never writes Codex state; the flag only reports that')
    p.add_argument('--keep-route',action='store_true',help='Close the session but keep the route registered')
    p.add_argument('--timeout',type=float,default=10)
    g = p.add_mutually_exclusive_group()
    g.add_argument('--retro-file')
    g.add_argument('--no-retro',metavar='REASON')
    p.add_argument('--card',default=None)
    p.add_argument('--tower')
    p.add_argument('--urgency',choices=['stop','repeat','note'],default='note')
    p.add_argument('--tags',default='')
    p.add_argument('--notify')
    p.add_argument('--store')
    p.add_argument('--state-dir')
    p = sub.add_parser('close-operator', help='Claim an authorized Codex native archive handoff from its enrolled chat')
    p.add_argument('id', metavar='MESSAGE_ID')


def add_record_commands(sub):
    """Owner-private records: retrospectives, run receipts and verified handovers."""
    p = sub.add_parser('retro',help='Append and inspect project retrospectives')
    retro_sub = p.add_subparsers(dest='retro_cmd',required=True)
    for action in ('add','list','tally'):
        r = retro_sub.add_parser(action)
        r.add_argument('--project',required=True)
        r.add_argument('--store')
        r.add_argument('--state-dir')
        r.add_argument('--json',action='store_true')
        if action == 'add':
            for field in ('card','runner','model','from-file','urgency','tags'):
                r.add_argument('--'+field,required=True,**({'choices':['stop','repeat','note']} if field=='urgency' else {}))
            r.add_argument('--tower')
            r.add_argument('--session',default=os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID'))
            r.add_argument('--notify')
        else:
            r.add_argument('--since')
            if action == 'list':
                r.add_argument('--tag')
                r.add_argument('--urgency',choices=['stop','repeat','note'])
    p = sub.add_parser('run',help='Capture a local command in an owner-private run receipt')
    p.add_argument('--label',required=True)
    p.add_argument('--cwd')
    p.add_argument('--state-dir')
    p.add_argument('command',nargs=argparse.REMAINDER)
    p = sub.add_parser('handover',help='Verify receipt-backed rows before one peer message')
    p.add_argument('action',nargs='?',choices=['check'])
    p.add_argument('--rows',required=True)
    p.add_argument('--head',required=True)
    p.add_argument('--state-dir')
    p.add_argument('--project')
    p.add_argument('--from',dest='sender')
    p.add_argument('--to',dest='recipient')
    p.add_argument('--card')
    p.add_argument('--note-file')


def add_lookup_commands(sub):
    """Reads of sessions, policy, routes and channels, and the two channel writes."""
    p = sub.add_parser('wake',help="Bring an idle-closed claude-peer session back in its own host; prints woken, already-live, unsupported, failed or uncertain")
    p.add_argument('name')
    p.add_argument('--project',required=True)
    p.add_argument('--timeout',type=float,default=sessions.WAKE_TIMEOUT,help='Seconds to wait for the woken session inbox')
    p = sub.add_parser('title',help="Label a claude-peer route's session with PROJECT/NAME in the resume picker (works on dead sessions)")
    p.add_argument('name')
    p.add_argument('--project',required=True)
    p.add_argument('--force',action='store_true',help='Replace a title the session already has')
    p = sub.add_parser('policy',help='Show the model policy (defaults, denylist) the bridge enforces; read-only')
    p.add_argument('--state-dir')
    p = sub.add_parser('codex-model',help="Deprecated UUID alias for model NAME --set; changes next-turn settings")
    p.add_argument('id',metavar='SESSION_ID')
    p.add_argument('--model',required=True)
    p.add_argument('--timeout',type=float,default=10)
    p.add_argument('--state-dir')
    p = sub.add_parser('sessions',help='Discover real CLI sessions without starting a turn')
    p.add_argument('--provider',choices=['all','codex','claude','agy'],default='all')
    p.add_argument('--cwd')
    p.add_argument('--search')
    p.add_argument('--limit',type=int,default=100)
    p.add_argument('--codex-executable',default='codex')
    p.add_argument('--claude-executable',default='claude')
    p.add_argument('--codex-mode',choices=['auto','shared','saved'],default='auto')
    model_options = p.add_mutually_exclusive_group()
    model_options.add_argument('--with-model',action='store_true',help='Read the live model for each discovered session')
    model_options.add_argument('--no-model',action='store_false',dest='with_model',help='Skip live model reads (default)')
    p.set_defaults(with_model=False)
    p.add_argument('--state-dir')
    p = sub.add_parser('session')
    p.add_argument('provider',choices=['codex','claude','agy'])
    p.add_argument('id')
    p.add_argument('--executable')
    p.add_argument('--codex-mode',choices=['auto','shared','saved'],default='auto')
    for cmd in ('routes','channels'):
        p = sub.add_parser(cmd)
        p.add_argument('--project',required=True)
        if cmd == 'routes':
            p.add_argument('--no-model',action='store_true',help='Skip the live model reading')
            p.add_argument('--state-dir')
    p = sub.add_parser('channel-bind')
    p.add_argument('name')
    p.add_argument('--project',required=True)
    p.add_argument('--owner',required=True)
    p.add_argument('--description',required=True)
    p.add_argument('--expect-generation',type=int,default=0)
    p = sub.add_parser('channel-close')
    p.add_argument('name')
    p.add_argument('--project',required=True)
    p.add_argument('--expect-generation',type=int,required=True)


def add_message_commands(sub):
    """Correspondence: send, ask, reply, the reads and the delivery outcomes."""
    for cmd in ('send','ask'):
        p = sub.add_parser(cmd)
        p.add_argument('--project',required=True)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument('--from',dest='sender')
        g.add_argument('--from-channel',dest='sender_channel')
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument('--to',dest='recipient')
        g.add_argument('--channel')
        p.add_argument('--file',required=True)
        p.add_argument('--key',required=True)
        g = p.add_mutually_exclusive_group()
        if cmd == 'send':
            g.add_argument('--one-way',action='store_true',help='Explicitly no answer requested')
        g.add_argument('--reply-deadline',dest='reply_deadline_seconds',type=float,help='Explicit reply lifetime in seconds (max 604800); local wait timeout is separate')
        p.add_argument('--state-dir',help=USAGE_STATE_HELP)
        if cmd=='ask':
            p.add_argument('--timeout',type=float,default=60)
    p = sub.add_parser('reply')
    p.add_argument('id')
    p.add_argument('--file',required=True)
    p.add_argument('--key',required=True)
    p.add_argument('--state-dir',help=USAGE_STATE_HELP)
    for cmd in ('watch','inbox'):
        p = sub.add_parser(cmd)
        p.add_argument('name',nargs='?')
        p.add_argument('--project',required=True)
        p.add_argument('--channel')
        if cmd=='watch':
            p.add_argument('--after',type=int)
            p.add_argument('--instance')
        else:
            p.add_argument('--session')
            p.add_argument('--all',action='store_true')
    p = sub.add_parser('wait')
    p.add_argument('id')
    p.add_argument('--timeout',type=float,default=60)
    for cmd in ('show','conversation','ack'):
        p = sub.add_parser(cmd)
        p.add_argument('id')
    p = sub.add_parser('receive-codex', help='Read one dispatched peer message from its addressed Codex thread')
    p.add_argument('id', metavar='MESSAGE_ID')
    p = sub.add_parser('progress')
    p.add_argument('id')
    p.add_argument('--note',required=True)
    p = sub.add_parser('deliver')
    p.add_argument('id')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--timeout',type=float,default=180)
    p = sub.add_parser('resolve')
    p.add_argument('id')
    p.add_argument('--state',choices=['pending','submitted','done'],required=True)
    p.add_argument('--note',required=True)
    p = sub.add_parser('wt',add_help=False,help='Agent worktrees: new, list, sweep, adopt (bridge.py wt --help)')
    p.add_argument('wt_args',nargs=argparse.REMAINDER)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket',default=str(broker.DEFAULT_SOCKET))
    sub = parser.add_subparsers(dest='cmd',required=True)
    add_broker_commands(sub)
    add_session_commands(sub)
    add_record_commands(sub)
    add_lookup_commands(sub)
    add_message_commands(sub)
    return parser


def run_command(args):
    return receipts.run(broker_state_dir(args), args.label, args.cwd, args.command)


def handover_command(args):
    checked = receipts.check(broker_state_dir(args), args.rows, args.head)
    if args.action == 'check':
        output({'verified': True, 'head': args.head, 'rows': checked})
        return 0
    if not all(getattr(args, name) and getattr(args, name).strip()
               for name in ('project', 'sender', 'recipient', 'card')):
        raise ValueError('HANDOVER_UNVERIFIED: send requires --project, --from, --to and --card')
    note = None
    if args.note_file:
        with Path(args.note_file).expanduser().open(encoding='utf-8', newline='') as source:
            note = source.read()
    body = receipts.message(args.card, args.head, checked, note)
    # Identical verified content has one durable broker idempotency key. No retry on uncertainty.
    key_data = json.dumps([args.project, args.sender, args.recipient, body], ensure_ascii=False).encode('utf-8')
    sent = broker.request(args.socket, {'op': 'send', 'project': args.project,
        'sender': args.sender, 'recipient': args.recipient, 'body': body,
        'key': 'handover-' + hashlib.sha256(key_data).hexdigest()})
    output(push_if_peer(args.socket, refuse_uncertain_replay(sent)))
    return 0


def serve_command(args):
    if args.max_messages<1:
        raise ValueError('Message limit must be positive')
    if args.usage_interval and args.usage_interval<usage.MIN_INTERVAL_MINUTES:
        raise ValueError(f'Usage interval must be 0 (off) or at least {usage.MIN_INTERVAL_MINUTES} minutes')
    asyncio.run(broker.serve(args.socket,args.max_messages,args.state_dir,
                             args.usage_interval,args.codex_executable,args.claude_live,
                             args.codex_failure_interval,
                             tuple(int(value) for value in args.provider_retry_backoff.split(',')),
                             args.provider_retry_cap,
                             args.agy_failure_interval, args.retention_ttl, args.retention_prepare))
    return 0


def retention_command(args):
    if args.live:
        output(broker.request(args.socket, {'op': 'retention-plan'}))
    else:
        if not 1 <= args.max_messages <= 2048:
            raise ValueError('TTL active-message limit must be 1..2048')
        store = broker.message_store.MessageStore(broker_state_dir(args), args.max_messages)
        output(store.retention_dry_run())
    return 0


def session_usage_command(args):
    if args.name:
        if not args.project or args.provider or args.id:
            raise ValueError('Use NAME --project P or --provider PROVIDER --id ID')
        route = registered_route(args)
        provider = {'claude-peer': 'claude', 'claude': 'claude', 'codex': 'codex', 'agy': 'agy'}.get(route['transport'])
        if provider is None:
            raise ValueError('Registered transport has no session usage reader')
        sid = route['session']
    else:
        if not args.provider or not args.id or args.project:
            raise ValueError('Use NAME --project P or --provider PROVIDER --id ID')
        provider, sid = args.provider, args.id
    result = session_usage.query(provider, sid, args.prices)
    if args.json:
        output(result)
    else:
        print(session_usage.format_line(result), flush=True)
    return 0


def usage_command(args):
    state_dir = args.state_dir or Path(args.socket).expanduser().absolute().with_suffix('.state')
    if args.seed_announcements:
        output(usage.seed_announcements(state_dir, args.seed_announcements))
        return 0
    if args.refresh and args.provider in ('all', 'agy'):
        usage.refresh_agy(state_dir)
    records, summary, moment = usage.query(state_dir, args.since, args.provider)
    warning = usage.claude_caller_warning(summary, args.provider)
    if warning and not args.json:
        print(warning, flush=True)  # First, before any Claude bucket (#125).
    extra = {} if warning is None else {'warning': warning}
    if args.events:
        if args.json:
            output(dict(**extra, announcements=summary['announcements'],
                        buckets=[{key: row[key] for key in ('limit_id','slot','resets_at','events','early_reset',
                                                            'spend_horizon','spend_basis','stop_risk_horizon')}
                                 for row in summary['buckets']]))
        else:
            print(usage.format_events(summary, summary['announcements']), flush=True)
    elif args.json:
        cutoff = moment-usage.parse_since(args.since)
        payload = {k: v for k, v in summary.items() if k != 'summaries'}
        output(dict(**extra, records=[r for r in records if usage.parse_time(r['observed_at'])>=cutoff],
                    **payload))
    else:
        print(usage.format_summary(summary, moment), flush=True)
    return 0


def service_command(args):
    fields = dict(socket=args.socket, state_dir=args.state_dir, plist=args.plist,
                  generate_only=args.generate_only)
    if args.cmd == 'install-service':
        fields.update(max_messages=args.max_messages, retention_prepare=args.retention_prepare, retention_ttl=args.retention_ttl)
    output((service.install if args.cmd == 'install-service' else service.uninstall)(**fields))
    return 0


def open_command(args):
    if args.provider == 'codex':
        return open_codex_result(args)
    if args.register or args.project or args.new:
        raise ValueError('--register, --project and --new on open apply only to --provider codex')
    new_session_policy(args, args.provider)
    result = sessions.open_session(args.provider,args.cwd,args.title,args.model,args.timeout,host=args.host,ide=args.ide)
    output(result)
    return 0


def retro_output_command(args):
    result = retro_command(args)
    if args.json or args.retro_cmd == 'add':
        output(result)
    elif args.retro_cmd == 'tally':
        for row in result:
            print(f"{row['tag']}\t{row['count']}\t{row['first']}\t{row['last']}\t{', '.join(str(c) for c in row['cards'])}")
    else:
        for row in result:
            print(f"{row['time']} {row['urgency']} {row['card']} {row['runner']} [{','.join(row['tags'])}]\n{row['text'] if row['text'] is not None else row['no_retro']}")
    return 0


def archive_sweep_command(args):
    if args.apply != (args.manifest is not None):
        raise ValueError('--apply and --manifest FILE must be supplied together')
    if not 0 < args.timeout <= 600:
        raise ValueError('Timeout must be between 0 and 600 seconds')
    path = codex_archive.sweep_store()
    directory = broker_state_dir(args).expanduser().resolve()
    prefix = codex_archive.absolute_path(args.cwd_prefix) if args.cwd_prefix is not None else None
    if not args.apply:
        output(codex_archive.plan(path, directory, prefix))
        return 0
    with args.manifest.expanduser().open(encoding='utf-8') as source:
        manifest = json.load(source)
    result = codex_archive.apply(path, directory, prefix, manifest, args.timeout)
    output(result)
    return int(not result['complete'])


def close_command(args):
    result = close_participant(args)
    output(result)
    return int(bool(result.get('cleanup_error') or result.get('operator_error') or result.get('archive_incomplete')))


def close_operator_command(args):
    caller = os.environ.get('CODEX_THREAD_ID')
    if not caller:
        raise ValueError('Run close-operator from the enrolled Codex Desktop chat')
    request = broker.request(args.socket, {'op':'close-operator', 'id':str(uuid.UUID(args.id)),
                              'operator_session':str(uuid.UUID(caller)),
                              'native_guard_version':broker.CLOSE_OPERATOR_GUARD_VERSION})
    target = request['target']
    if 'native_scope' in request:
        codex_archive.check_native_operator_processes(target, request['native_scope'])
    output({key: target[key] for key in ('project', 'name', 'session', 'cwd')}
           | {'close_id': request['close_id']})
    return 0


def receive_codex_command(args):
    output(receive_codex(args))
    return 0


def wake_command(args):
    result = wake_participant(args)
    output(result)
    return 0 if result['state'] in ('woken','already-live') else 1


def title_command(args):
    route = registered_route(args)
    if route['transport'] != 'claude-peer':
        raise models.Unsupported(f"{route['transport']}: titles are set at creation; only claude-peer routes are labelled")
    found = label_claude(route, force=args.force)
    output({'name':args.name,'project':args.project,'session':route['session'],**found})
    return 0 if found['state'] != 'skipped' else 1


def policy_command(args):
    output(models.load_policy(broker_state_dir(args)))
    return 0


def model_command(args):
    if not 0 < args.timeout <= 3600:
        raise ValueError('Timeout must be between 0 and 3600 seconds')
    policy = models.load_policy(broker_state_dir(args))
    route = registered_route(args)
    if args.target is None:
        output({'name':args.name,**models.flag(policy, models.read(route, min(args.timeout, 30)))})
        return 0
    output({'name':args.name,**models.switch(route, args.target, policy, args.timeout, ModelRelay(args.socket))})
    return 0


def codex_model_command(args):
    policy = models.load_policy(broker_state_dir(args))
    route = {'transport':'codex','session':args.id}
    output(models.switch(route, args.model, policy, args.timeout))
    return 0


def sessions_command(args):
    result = sessions.discover(args.provider,args.cwd,args.search,args.limit,args.codex_executable,args.claude_executable,args.codex_mode)
    if args.with_model:
        policy = models.load_policy(broker_state_dir(args))
        for name in ('codex','agy','claude'):
            if name in result['providers']:
                with_models(result['providers'][name]['sessions'], policy, 10)
    output(result)
    return 1 if result['errors'] else 0


def session_command(args):
    output(sessions.inspect_session(args.provider,args.id,args.executable,args.codex_mode))
    return 0


REQUEST_OPS = {'stop': 'shutdown', 'ask': 'send'}
NOT_REQUEST_FIELDS = ('cmd', 'socket', 'file', 'discover', 'new', 'title', 'model', 'codex_mode', 'state_dir', 'no_model')


def request_fields(args):
    """The broker request a plain command sends: its own options under its operation name."""
    fields = {key: value for key, value in vars(args).items()
              if key not in NOT_REQUEST_FIELDS and not (key == 'timeout' and args.cmd == 'register')}
    fields['op'] = REQUEST_OPS.get(args.cmd, args.cmd)
    return fields


def message_fields(args):
    """request_fields for send, ask and reply, with the body read from --file."""
    fields = request_fields(args)
    if args.cmd in ('send', 'ask') and (fields.get('one_way') or fields.get('reply_deadline_seconds') is not None):
        # A distinct operation is rejected before mutation by older brokers,
        # including when the broker restarts between separate CLI calls.
        fields['op'] = 'send-with-retention'
    fields['body'] = sys.stdin.read() if args.file == '-' else Path(args.file).read_text(encoding='utf-8')
    return fields


def require_wait_timeout(args):
    if not 0 < args.timeout <= 3600:
        raise ValueError('Timeout must be between 0 and 3600 seconds')


def request_command(args):
    output(broker.request(args.socket, request_fields(args)))
    return 0


READ_STATES = frozenset(('received', 'in_progress', 'answered', 'done'))


def show_command(args):
    """`show ID`, plus whether a Codex recipient's thread has retrieved the message (issue #153).

    For a Codex route `submitted` only means the queue accepted the notice; `receive-codex`
    moves it to `received`. Derived here at print time; the broker payload is unchanged.
    """
    result = broker.request(args.socket, request_fields(args))
    route = result.get('route') if isinstance(result, dict) else None
    if isinstance(route, dict) and route.get('transport') == 'codex':
        if result.get('state') == 'submitted':
            result['read_by_recipient'] = False
        elif result.get('state') in READ_STATES:
            result['read_by_recipient'] = True
    output(result)
    return 0


CODEX_HEALTH_SECONDS = 2


def codex_delivery_health(budget=CODEX_HEALTH_SECONDS):
    """Which path a Codex delivery would take now, checked client-side within `budget` seconds.

    Read-only: connects to the legacy IPC socket and closes it without sending, then checks the
    Desktop queue store and its single owner. No codesign. The reason is a fixed string; paths,
    pids and ids never appear. Never raises.
    """
    deadline = time.monotonic() + budget
    try:
        client = codex_live.Client(timeout=budget / 4)
        try:
            client.connect()
            return {'path': 'ipc', 'reason': None}
        except codex_live.EndpointUnavailable as error:
            # Only a missing or unserved default socket falls back to the queue; delivery fails otherwise.
            if error.endpoint_name != error.DEFAULT:
                return {'path': 'unavailable', 'reason': 'non-default Codex configuration'}
            if not error.default_absent:
                return {'path': 'unavailable', 'reason': 'Codex IPC socket unusable'}
        finally:
            client.close()
        try:
            _, queue = codex_queue.default_stores()
        except ValueError:
            return {'path': 'unavailable', 'reason': 'non-default Codex configuration'}
        try:
            codex_preview.require_owned_store(queue)
        except (OSError, ValueError):
            return {'path': 'unavailable', 'reason': 'queue store missing or not owned'}

        def read(argv):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, 0)
            return codex_queue.checked_output(argv, timeout=remaining)

        try:
            owner = codex_queue.desktop_queue_owner(queue, read)
        except subprocess.TimeoutExpired:
            return {'path': 'unavailable', 'reason': 'queue owner check timed out'}
        except (OSError, subprocess.SubprocessError, ValueError):
            owner = None
        if owner is None:  # Also a second app-server (another tool's `codex app-server`) holding the store.
            return {'path': 'unavailable', 'reason': 'no single Desktop queue owner'}
        return {'path': 'desktop-queue', 'reason': None}
    except Exception:
        return {'path': 'unavailable', 'reason': 'health check failed'}


def close_operator_health(args, budget=CODEX_HEALTH_SECONDS):
    """Whether an enrolled Codex close operator could take a handoff now (#196). Never raises.

    `none` when nobody is enrolled. A dry-run delivery checks the operator chat exists, is not
    archived and has a delivery path; the reason is a fixed string, never an id or path.
    """
    try:
        status = broker.request(args.socket, {'op': 'close-operator-status', 'explicit': True})
    except ValueError as error:
        reason = 'broker cannot report it' if str(error) == 'Unknown operation' else 'enrollment invalid'
        return {'state': 'unavailable', 'reason': reason}
    except Exception:
        return {'state': 'unavailable', 'reason': 'health check failed'}
    operator = status.get('operator') if isinstance(status, dict) else None
    if operator is None:
        return {'state': 'none', 'reason': None}
    try:
        codex_live.deliver(operator['route'], str(uuid.uuid4()), '', budget, dry_run=True,
                           allow_queue=Path(args.socket).expanduser() == broker.DEFAULT_SOCKET, close_handoff=True)
    except codex_live.ThreadArchived:
        return {'state': 'unavailable', 'reason': 'operator chat is archived'}
    except codex_live.EndpointUnavailable:
        return {'state': 'unavailable', 'reason': 'no Codex delivery path'}
    except Exception:
        return {'state': 'unavailable', 'reason': 'operator chat cannot be reached'}
    return {'state': 'ok', 'reason': None}


def ping_command(args):
    result = broker.request(args.socket, request_fields(args))
    result['cli_build'] = broker.build_id()
    warning = build_skew(args.socket, result)
    if warning is not None:
        result['warning'] = warning
    if sys.platform == 'darwin':
        result['codex_delivery'] = codex_delivery_health()
        result['close_operator'] = close_operator_health(args)
    output(result)
    return 0


def routes_command(args):
    result = broker.request(args.socket, request_fields(args))
    if not args.no_model:
        with_models(result, models.load_policy(broker_state_dir(args)), 5)
    output(result)
    return 0


def register_command(args):
    if args.transport == 'codex' and args.new:
        return open_codex_result(args)
    if args.new and args.transport in ('agy', 'claude-peer'):
        validate_new_registration(args)
        require_free_registration_name(args.socket, args.project, args.name)
        new_session_policy(args, 'claude' if args.transport == 'claude-peer' else args.transport)
    observation = registration(args)  # Fills args.session and args.cwd before the request is built.
    result = broker.request(args.socket, request_fields(args))
    if observation:
        result['discovery'] = observation
    if args.transport == 'claude-peer' and result.get('session'):
        result['title'] = label_claude(result)
    output(result)
    return 0


def deliver_command(args):
    result = deliver(args.socket, args.id, args.timeout, args.dry_run)
    output(result)
    return int(result['state'] in ('failed', 'uncertain') or result.get('reason') == ARCHIVED_REASON)


def watch_command(args):
    for event in broker.stream(args.socket, request_fields(args)):
        output(event)
    return 0


def refuse_uncertain_replay(sent):
    """A new message is always pending, so an uncertain claude-peer one is a same-key replay (#200).

    Answer like the first call did (exit 1, uncertain), so a caller retrying after a lost response
    never reads success. Nothing is pushed and nothing waits.
    """
    if sent['state'] == 'uncertain' and sent['route']['transport'] == 'claude-peer':
        raise ClaudeDispatchUnknown('replay', sent['id'])
    return sent


def send_command(args):
    sent = refuse_uncertain_replay(broker.request(args.socket, message_fields(args)))
    output(with_usage(args, push_if_peer(args.socket, sent)))
    return 0


def wait_result(args, fields):
    result = broker.request(args.socket, fields, timeout=args.timeout + 5)
    output(result)
    return 2 if result['timed_out'] else 0


def ask_command(args):
    fields = message_fields(args)
    require_wait_timeout(args)
    sent = push_if_peer(args.socket, refuse_uncertain_replay(broker.request(args.socket, fields)))
    found = message_usage(args, sent)
    output({'type': 'sent', 'message': sent} | ({} if found is None else {'usage': found}))
    return wait_result(args, {'op': 'wait', 'id': sent['id'], 'timeout': args.timeout})


def wait_command(args):
    require_wait_timeout(args)
    return wait_result(args, request_fields(args))


COMMANDS = {
    'run': run_command, 'handover': handover_command, 'serve': serve_command,
    'retention': retention_command, 'session-usage': session_usage_command, 'usage': usage_command,
    'install-service': service_command, 'uninstall-service': service_command,
    'open': open_command, 'register': register_command,
    'retro': retro_output_command, 'archive-sweep': archive_sweep_command, 'close': close_command, 'close-operator': close_operator_command,
    'receive-codex': receive_codex_command, 'wake': wake_command, 'title': title_command,
    'policy': policy_command, 'model': model_command, 'codex-model': codex_model_command,
    'sessions': sessions_command, 'session': session_command,
    'deliver': deliver_command, 'watch': watch_command,
    'send': send_command, 'reply': send_command, 'ask': ask_command, 'wait': wait_command,
    'ping': ping_command, 'routes': routes_command, 'show': show_command,
    # Plain requests: the parsed options are the broker operation.
    **{name: request_command for name in ('stop', 'channels', 'channel-bind', 'channel-close', 'inbox',
                                          'conversation', 'ack', 'progress', 'resolve')},
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['wt']:  # Own parser, so `wt --help` and leading options reach it.
        return wt.main(argv[1:],prog='bridge.py wt')
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd=='wt':
        return wt.main(args.wt_args,prog='bridge.py wt')
    try:
        return COMMANDS[args.cmd](args)
    except KeyboardInterrupt:
        return 130
    except ClaudeDispatchUnknown as error:
        output({'error':str(error), 'id':error.mid, 'stage':error.stage, 'uncertain':True})
        return 1
    except PushFailed as error:
        message = error.message
        if message['route']['transport'] == 'codex':
            message = {k:message[k] for k in ('id','state','route')}
        result = {'error':str(error),'push_failed':True,'message':message}
        if error.message.get('reason') == ARCHIVED_REASON:
            message['reason'] = result['reason'] = ARCHIVED_REASON
        found = message_usage(args, error.message)
        output(result if found is None else {**result, 'usage': found})
        return 1
    except models.Unsupported as error:
        output({'error':str(error),'unsupported':True})
        return 1
    except agy_live.ArchiveUncertain as error:
        output({'error':str(error),'uncertain':True,'name':args.name})
        return 1
    except models.SwitchError as error:
        if args.cmd == 'codex-model':
            output({'error':str(error),'uncertain':error.uncertain,'session':args.id,'model':args.model})
            return 1
        output({'error':str(error),'uncertain':error.uncertain,'name':args.name,'model':args.target})
        return 1
    except codex_new.ModelSwitchError as error:
        output({'error':str(error),'uncertain':error.uncertain,'session':args.id,'model':args.model})
        return 1
    except codex_new.CreationError as error:
        output({'error':str(error),'uncertain':error.uncertain,'session':error.session,
                'created':error.created,'adopted':error.adopted,'registered':False,'stage':error.stage})
        return 130 if error.cancelled else 1
    except (ValueError,OSError,subprocess.TimeoutExpired,KeyError,TypeError) as error:
        output(with_skew(args, {'error':str(error)}, error))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
