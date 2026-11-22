"""Provider-neutral model read and switch for registered routes (issue #13).

One command, `model NAME [--set M]`, resolves the participant's route and hands
it to the adapter registered for the route's transport. Adapters live with
their provider; this module only routes, applies the owner's policy, and
reports a provider gap by name. It never fakes a switch it cannot make.
"""
import json
import os
import re
import time
from pathlib import Path
import uuid

import agy_live
import sessions
import codex_live
import codex_new
import codex_wake_open
from new_session_policy import (CODEX_DESKTOP_WAKE_PERMISSIONS, POLICY_FILE, SEED_DENY, default_model, denied,
                                env_name, load_policy, require_allowed)

PROVIDER = {'codex': 'codex', 'agy': 'agy', 'claude-peer': 'claude', 'claude': 'claude'}


class Unsupported(ValueError):
    """A provider gap: no reachable mechanism. Reported, never worked around."""


class SwitchError(ValueError):
    def __init__(self, reason, uncertain=False):
        super().__init__(reason)
        self.uncertain = uncertain


# --- Policy: per-provider defaults and the denylist -------------------------
# A person edits <state-dir>/policy.json; the bridge only reads it.
#   {"defaults": {"agy": "gemini-4-argon-medium"}, "deny": {"codex": ["gpt-6-astra"]},
#    "command_post": {"claude": "hq"}}
# "command_post" names, per provider, the registered route that performs a
# switch the bridge cannot make itself (Claude desktop: see claude_desktop_switch).


def new_session_policy(args, provider, state_dir):
    """Apply the owner's defaults and denylist before any session is created."""
    policy = load_policy(state_dir)
    if provider == 'codex':
        args.model, source = codex_new.resolve_model(args.model, policy=policy)
        args.queue_wake_workspace_write = CODEX_DESKTOP_WAKE_PERMISSIONS in policy
        return source
    model, source = default_model(provider, args.model, policy)
    if provider == 'claude':
        if model is not None:
            raise Unsupported(f'claude: a new session cannot be given a model ({source} asked for {model!r}). '
                              'Neither the desktop new-session link nor the IDE extension link carries a model; '
                              'the session starts on the model its host picks.')
        return
    host = getattr(args, 'host', 'desktop')
    cwd = getattr(args, 'cwd', None)
    server = agy_live.discover_host_server(host, cwd=cwd) if cwd else agy_live.discover_host_server(host)
    if model is not None:
        found = agy_live.resolve_model(server, model)
        require_allowed(policy, provider, model, found['model_id'], found['model_enum'], found.get('label'))
        args.model = found['model_id']
    else:
        found = agy_live.resolve_model(server, None)
        require_allowed(policy, provider, found['model_id'], found['model_enum'], found.get('label'))


# MCP session creation catalog. CLI resolution keeps its existing behavior.
SESSION_MODEL_LIMIT = 200
SESSION_MODEL_ID_LIMIT = 128
SESSION_MODEL_LABEL_LIMIT = 256
SESSION_MODEL_TIMEOUT = 5
_CATALOG_LIMIT = 10000
_MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_./:-]*')
_CATALOG_UNAVAILABLE = 'Session model catalog unavailable'
_MODEL_UNAVAILABLE = 'Requested model is unavailable for this agent type'


class ModelSelectionError(ValueError):
    """Public MCP model-selection refusal with a fixed message."""


def _model_option(model_id, label):
    if (not isinstance(model_id, str) or len(model_id) > SESSION_MODEL_ID_LIMIT
            or not _MODEL_ID.fullmatch(model_id)):
        return None
    if (not isinstance(label, str) or not label.strip() or len(label) > SESSION_MODEL_LABEL_LIMIT
            or not label.isprintable()):
        label = model_id
    return {'id': model_id, 'label': label}


def _codex_options(policy, remaining):
    options = {}
    blocked = set()
    with sessions.CodexClient(mode='saved', timeout=remaining(), experimental=True) as client:
        for count, row in enumerate(codex_new.model_catalog(client, remaining), 1):
            if count > _CATALOG_LIMIT:
                raise ModelSelectionError(_CATALOG_UNAVAILABLE)
            label = row.get('displayName')
            option = _model_option(row.get('model'), label)
            if option:
                model_id = option['id']
                if denied(policy, 'codex', model_id, row.get('id'), label):
                    blocked.add(model_id)
                options.setdefault(model_id, option)
    return {model_id: option for model_id, option in options.items() if model_id not in blocked}


def _agy_options(policy, catalog):
    if len(catalog) > _CATALOG_LIMIT:
        raise ModelSelectionError(_CATALOG_UNAVAILABLE)
    matches = {}
    rows = []
    for row in catalog:
        model_or_alias = row.get('modelOrAlias', {})
        if not isinstance(model_or_alias, dict):
            raise ModelSelectionError(_CATALOG_UNAVAILABLE)
        model_enum = model_or_alias.get('model')
        if not isinstance(model_enum, str) or not model_enum:
            continue
        model_id, label = row.get('modelId'), row.get('label')
        # The local resolver matches these names case-insensitively in catalog order.
        # Exclude advertised IDs that could resolve to a different catalog row.
        names = (model_id, model_enum, label)
        identity = (model_id if isinstance(model_id, str) else None, model_enum)
        for name in names:
            if isinstance(name, str):
                matches.setdefault(name.casefold(), set()).add(identity)
        rows.append((identity, _model_option(model_id, label),
                     denied(policy, 'agy', *names, model_or_alias.get('alias'))))
    denied_ids = {identity for identity, _, blocked in rows if blocked}
    options = {}
    for identity, option, _ in rows:
        # A catalog change during open must not send the CLI's raw enum fallback.
        if (option and not option['id'].startswith('MODEL_') and identity not in denied_ids
                and matches[option['id'].casefold()] == {identity}):
            options.setdefault(option['id'], option)
    return options


def _session_model_options(workspace, agent_type, state_dir, include_default):
    result = {'supports_model_selection': agent_type != 'claude', 'default_model': None, 'models': []}
    if agent_type == 'claude':
        return result
    if agent_type not in ('codex', 'agy'):
        raise ModelSelectionError(_MODEL_UNAVAILABLE)

    deadline = time.monotonic() + SESSION_MODEL_TIMEOUT

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise ModelSelectionError(_CATALOG_UNAVAILABLE)
        return left

    try:
        policy = load_policy(state_dir)
        if agent_type == 'codex':
            options = _codex_options(policy, remaining)
        else:
            server = agy_live.discover_host_server(workspace.get('host', 'desktop'),
                                                   cwd=workspace['cwd'], timeout=remaining())
            options = _agy_options(policy, agy_live.model_catalog(server, timeout=remaining()))
    except (ValueError, OSError, KeyError, TypeError):
        raise ModelSelectionError(_CATALOG_UNAVAILABLE) from None
    result['models'] = list(options.values())
    if include_default:
        result['models'] = result['models'][:SESSION_MODEL_LIMIT]
    if include_default and options:
        try:
            chosen, _ = default_model(agent_type, None, policy)
            if agent_type == 'agy':
                found = agy_live.resolve_model(server, chosen, timeout=remaining())
                if denied(policy, 'agy', chosen, found['model_id'], found['model_enum'], found.get('label')):
                    chosen = None
                else:
                    chosen = found['model_id']
            if chosen in options:
                result['default_model'] = chosen
                if all(row['id'] != chosen for row in result['models']):
                    result['models'][-1] = options[chosen]
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            pass  # Explicit choices remain available if the default cannot be resolved.
    return result


def session_model_options(workspace, agent_type, state_dir):
    """Return the read-only public model catalog and effective allowed default."""
    return _session_model_options(workspace, agent_type, state_dir, include_default=True)


def explicit_session_model(workspace, agent_type, state_dir, requested):
    """Validate an exact advertised canonical ID before a remote session starts."""
    if agent_type == 'claude' or _model_option(requested, None) is None:
        raise ModelSelectionError(_MODEL_UNAVAILABLE)
    result = _session_model_options(workspace, agent_type, state_dir, include_default=False)
    if any(row['id'] == requested for row in result['models']):
        return requested
    raise ModelSelectionError(_MODEL_UNAVAILABLE)


# --- Adapters ----------------------------------------------------------------
# An adapter is read(route, timeout) -> dict and switch(route, model, timeout)
# -> dict, registered per transport. A missing piece raises Unsupported.

def codex_read(route, timeout):
    """Read the exact owner's resumed settings, including before a first turn."""
    sid = str(uuid.UUID(str(route['session'])))
    try:
        with codex_live.Client(timeout=timeout) as client:
            owner = client.owner(sid)
            settings = codex_new.owner_settings(client, sid, owner)
    except codex_live.EndpointUnavailable as error:
        if not error.default_absent:
            raise
        model = codex_wake_open.read_saved_model(route)
        return {'session': sid, 'model': model,
                'source': 'Desktop rollout turn_context (last recorded turn)'}
    return {'session': sid, 'model': settings['model'],
            'source': 'Desktop owner resumed model settings (next turn)'}


def codex_switch(route, model, timeout, **_):
    try:
        return codex_new.set_model(route['session'], model, timeout=timeout)
    except codex_new.ModelSwitchError as error:
        raise SwitchError(str(error), error.uncertain) from None


def codex_names(route, model, timeout):
    # #12 accepts only the exact model/list model string, never aliases. Its
    # switch performs catalog validation before contacting the Desktop owner.
    return [model]


def agy_read(route, timeout):
    return agy_live.read_model(route['session'], timeout=timeout)


def agy_switch(route, model, timeout, **_):
    try:
        return agy_live.set_model(route['session'], model, timeout=timeout)
    except agy_live.ModelSwitchError as error:
        raise SwitchError(str(error), error.uncertain) from None


def agy_names(route, model, timeout):
    """Every name a requested AGY model goes by, for the denylist."""
    server, _ = agy_live.resolve_agy(route['session'], timeout=timeout)
    found = agy_live.resolve_model(server, model, timeout=timeout)
    return [model, found['model_id'], found['model_enum'], found.get('label')]


def claude_projects_dir():
    base = os.environ.get('CLAUDE_CONFIG_DIR')
    return (Path(base).expanduser() if base else Path.home() / '.claude') / 'projects'


def claude_record(sid, sessions_dir=None):
    """The registry record of a live Claude session ({} when unreadable)."""
    row = sessions.resolve_claude_peer(sid, sessions_dir)
    try:
        record = json.loads(Path(row['registry']).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        record = {}
    return record if isinstance(record, dict) else {}


def claude_host(sid, sessions_dir=None):
    """The host that owns a live Claude session, from its registry record."""
    return claude_record(sid, sessions_dir).get('entrypoint') or 'unknown'


def desktop_session(sid):
    """The app's own id (hostSessionId, local_...) for a desktop session, else None."""
    try:
        host_id = claude_record(sid).get('hostSessionId')
    except sessions.DiscoveryError:
        return None
    return host_id if isinstance(host_id, str) and host_id.startswith('local_') else None


def transcript_model(sid, projects_dir=None, chunk=262144):
    """The model of the newest assistant message in the session transcript."""
    sid = str(uuid.UUID(sid))
    root = Path(projects_dir) if projects_dir else claude_projects_dir()
    paths = sorted(root.glob(f'*/{sid}.jsonl'))
    if len(paths) != 1:
        return None, ('no transcript' if not paths else f'{len(paths)} transcripts') + f' for {sid}'
    size = paths[0].stat().st_size
    with paths[0].open('rb') as handle:
        span = chunk
        while True:
            start = max(0, size - span)
            handle.seek(start)
            lines = handle.read(size - start).split(b'\n')
            if start:
                lines = lines[1:]  # the first line may be cut
            for line in reversed(lines):
                if b'"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get('message') if isinstance(entry, dict) else None
                model = message.get('model') if isinstance(message, dict) else None
                if entry.get('type') == 'assistant' and isinstance(model, str) and model and not model.startswith('<'):
                    return model, None
            if not start:
                return None, 'no assistant message yet'
            span *= 4


def claude_read(route, timeout):
    model, reason = transcript_model(route['session'])
    result = {'session': route['session']}
    try:
        result['host'] = claude_host(route['session'])
    except sessions.DiscoveryError:
        result['host'] = 'not running'
    if not model:
        return {**result, 'model': 'unknown', 'reason': reason}
    return {**result, 'model': model, 'source': 'transcript: newest assistant message (the model it last ran on)'}


# Evidence for these gaps: references/usage.md, "Models: Read, Switch, Policy".
CLAUDE_IDE_GAP = ('The VS Code extension switches a session only on its own stdio control channel '
                  '(applyFlagSettings) and saves the choice to the user-global settings.json; a running '
                  'session does not pick up a settings file change. Nothing outside the editor can switch '
                  'one IDE session. Use the model picker in that session.')
CLAUDE_HOST_GAP = {'claude-vscode': CLAUDE_IDE_GAP}

# --- Claude desktop: relay through a desktop command post --------------------
# The app serves ccd_session_mgmt set_session_model as an in-process SDK MCP
# server, reached only over the stdio pipe of a CLI the app itself spawned; the
# caller is whichever session owns that pipe. No process outside the app can
# call it. So the bridge asks a desktop-hosted Claude session that holds the
# tool (the command post named in policy.json) to make the call and reply.
REQUEST_PREFIX = 'bridge-model-request:'
REPLY_PREFIX = 'bridge-model-reply:'
RELAY_NAME = 'model-relay'  # realtime sender route: replies to it are never pushed


def request_body(request):
    return REQUEST_PREFIX + ' ' + json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def parse_reply(body):
    """The reply object from the first `bridge-model-reply:` line, else None."""
    for line in (body or '').splitlines():
        line = line.strip()
        if line.startswith(REPLY_PREFIX):
            try:
                value = json.loads(line[len(REPLY_PREFIX):])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def command_post(route, policy, relay):
    """The command post route for a Claude desktop switch, or Unsupported naming what to set."""
    name = (policy.get('command_post') or {}).get('claude')
    if not name:
        raise Unsupported(
            'claude (claude-desktop): the switch is made by the app\'s set_session_model tool, which only a '
            'desktop-hosted Claude session can call. No command post is configured: register that session as a '
            f'claude-peer route in project {route["project"]!r} and set "command_post": {{"claude": "<route name>"}} '
            f'in {policy["path"]} (a person edits that file).')
    post = next((r for r in relay.routes(route['project']) if r['name'] == name), None)
    where = f'command post {name!r} (policy {policy["path"]})'
    if post is None:
        raise Unsupported(f'claude (claude-desktop): {where} is not registered in project {route["project"]!r}')
    if post.get('transport') != 'claude-peer':
        raise Unsupported(f'claude (claude-desktop): {where} has transport {post.get("transport")!r}; '
                          'it must be a claude-peer route')
    if post['session'] == route['session']:
        raise Unsupported(f'claude (claude-desktop): {where} is the session being switched; the app refuses '
                          'set_session_model on the calling session. Use its model menu, or name another command post.')
    try:
        host = claude_host(post['session'])
    except sessions.DiscoveryError as error:
        raise Unsupported(f'claude (claude-desktop): {where} is not running: {error}') from None
    if host != 'claude-desktop':
        raise Unsupported(f'claude (claude-desktop): {where} runs on {host!r}; only a desktop-hosted session '
                          'has set_session_model')
    return post


def claude_desktop_switch(route, model, timeout, policy, relay):
    post = command_post(route, policy, relay)
    request_id = str(uuid.uuid4())
    request = {'v': 1, 'op': 'set_session_model', 'id': request_id, 'session_id': route['session'],
               'model': model, 'target': route['name'], 'project': route['project'], 'reply': REPLY_PREFIX}
    host_id = desktop_session(route['session'])
    if host_id:
        request['desktop_session'] = host_id
    body = request_body(request)
    sent, waited = relay.ask(route['project'], post['name'], post['cwd'], body,
                             f'model-request:{request_id}', timeout)
    via = {'via': post['name'], 'request': sent['id']}
    if waited.get('timed_out') or not waited.get('replies'):
        raise SwitchError(f'command post {post["name"]!r} did not answer request {sent["id"]} within {timeout:g}s '
                          '(the app may be waiting on an approval card). The switch may still happen: read the '
                          'model before asking again; never resend on a timeout.', uncertain=True)
    reply = parse_reply(waited['replies'][-1]['body'])
    if reply is None:
        raise SwitchError(f'command post {post["name"]!r} answered request {sent["id"]} without a '
                          f'{REPLY_PREFIX} line; check the target session before asking again', uncertain=True)
    if reply.get('error'):
        raise SwitchError(f'command post {post["name"]!r}: {reply["error"]}', bool(reply.get('uncertain')))
    if not isinstance(reply.get('model'), str) or not reply['model'] or not isinstance(reply.get('changed'), bool):
        raise SwitchError(f'command post {post["name"]!r} answered request {sent["id"]} with no model/changed '
                          'reading; check the target session before asking again', uncertain=True)
    result = {'session': route['session'], 'model': reply['model'], 'changed': reply['changed'], **via}
    if isinstance(reply.get('applies'), str):
        result['applies'] = reply['applies']
    return result


def claude_switch(route, model, timeout, policy=None, relay=None):
    host = claude_host(route['session'])
    if host == 'claude-desktop':
        if relay is None or policy is None:
            raise Unsupported('claude (claude-desktop): a desktop switch is relayed through a command post; '
                              'this caller supplied no bridge relay')
        return claude_desktop_switch(route, model, timeout, policy, relay)
    raise Unsupported(f'claude ({host}): ' + CLAUDE_HOST_GAP.get(
        host, 'no bridge-reachable model switch is known for this Claude host.'))


ADAPTERS = {
    'codex': {'read': codex_read, 'switch': codex_switch, 'names': codex_names},
    'agy': {'read': agy_read, 'switch': agy_switch, 'names': agy_names},
    'claude-peer': {'read': claude_read, 'switch': claude_switch},
    'claude': {'read': claude_read, 'switch': claude_switch},
}


def adapter(route, piece):
    transport = route.get('transport')
    if transport == 'realtime':
        raise Unsupported('realtime routes are CLI workers with no provider session; they have no model to read')
    found = ADAPTERS.get(transport, {}).get(piece)
    if not found:
        raise Unsupported(f'No {piece} adapter is registered for {PROVIDER.get(transport, transport)} yet')
    return found


def read(route, timeout=10):
    """Fresh at query time; 'unknown' with a reason when it cannot be read."""
    provider = PROVIDER.get(route.get('transport'), route.get('transport'))
    try:
        result = adapter(route, 'read')(route, timeout)
    except Unsupported as error:
        return {'provider': provider, 'model': 'unknown', 'reason': str(error)}
    except (ValueError, OSError, KeyError, TypeError) as error:
        return {'provider': provider, 'model': 'unknown', 'reason': str(error)}
    return {'provider': provider, **result}


def flag(policy, reading):
    provider = reading.get('provider')
    hit = denied(policy, provider, reading.get('model'), reading.get('model_enum'), reading.get('label'))
    return {**reading, 'denied': bool(hit)}


def switch(route, model, policy, timeout=30, relay=None):
    """relay: the bridge's message path, for adapters that ask another session to switch."""
    provider = PROVIDER.get(route.get('transport'), route.get('transport'))
    run = adapter(route, 'switch')
    names = adapter_names(route, model, timeout)
    require_allowed(policy, provider, *names)
    return {'provider': provider, **run(route, model, timeout, policy=policy, relay=relay)}


def adapter_names(route, model, timeout):
    found = ADAPTERS.get(route.get('transport'), {}).get('names')
    return found(route, model, min(timeout, 10)) if found else [model]
