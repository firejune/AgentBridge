"""Provider-neutral model read and switch for registered routes (issue #13).

One command, `model NAME [--set M]`, resolves the participant's route and hands
it to the adapter registered for the route's transport. Adapters live with
their provider; this module only routes, applies the owner's policy, and
reports a provider gap by name. It never fakes a switch it cannot make.
"""
import json
import os
from pathlib import Path
import uuid

import agy_live
import sessions

POLICY_FILE = 'policy.json'
# Seed from the owner's rule of 2026-10-23; applies only while no policy file exists.
SEED_DENY = {'codex': ['gpt-6-astra']}
PROVIDER = {'codex': 'codex', 'agy': 'agy', 'claude-peer': 'claude', 'claude': 'claude'}


class Unsupported(ValueError):
    """A provider gap: no reachable mechanism. Reported, never worked around."""


class SwitchError(ValueError):
    def __init__(self, reason, uncertain=False):
        super().__init__(reason)
        self.uncertain = uncertain


# --- Policy: per-provider defaults and the denylist -------------------------
# A person edits <state-dir>/policy.json; the bridge only reads it.
#   {"defaults": {"agy": "gemini-4-argon-medium"}, "deny": {"codex": ["gpt-6-astra"]}}

def load_policy(state_dir):
    path = Path(state_dir).expanduser() / POLICY_FILE
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {'path': str(path), 'exists': False, 'defaults': {}, 'deny': SEED_DENY}
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ValueError(f'{path} is not valid JSON: {error}') from None
    if not isinstance(data, dict):
        raise ValueError(f'{path} must hold a JSON object')
    defaults, deny = data.get('defaults', {}), data.get('deny', {})
    if not isinstance(defaults, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                                 for k, v in defaults.items()):
        raise ValueError(f'{path}: "defaults" must map a provider to a model name')
    if not isinstance(deny, dict) or not all(isinstance(k, str) and isinstance(v, list)
                                             and all(isinstance(m, str) and m.strip() for m in v)
                                             for k, v in deny.items()):
        raise ValueError(f'{path}: "deny" must map a provider to a list of model names')
    return {'path': str(path), 'exists': True, 'defaults': defaults, 'deny': deny}


def denied(policy, provider, *names):
    """The first name (model id, enum or label) on the provider's denylist, else None."""
    banned = {m.strip().casefold() for m in policy['deny'].get(provider, [])}
    return next((n for n in names if isinstance(n, str) and n.strip().casefold() in banned), None)


def require_allowed(policy, provider, *names):
    hit = denied(policy, provider, *names)
    if hit:
        where = policy['path'] if policy['exists'] else f"the built-in seed (no {policy['path']})"
        raise ValueError(f'Model {hit!r} is on the {provider} denylist in {where}; '
                         'only a person edits that list')


def env_name(provider):
    return f'AGENT_BRIDGE_{provider.upper()}_MODEL'


def default_model(provider, model=None, policy=None, environ=None):
    """--model, then AGENT_BRIDGE_<PROVIDER>_MODEL, then the policy default; (model, source).

    None means the provider's own default. Codex keeps its #12 resolution in
    codex_new until its adapter adopts this one.
    """
    if model is not None:
        return model, '--model'
    name = env_name(provider)
    value = (os.environ if environ is None else environ).get(name)
    if value is not None:
        if not value.strip():
            raise ValueError(f'{name} is set but empty; unset it or pass --model')
        return value, name
    if policy and policy['defaults'].get(provider):
        return policy['defaults'][provider], 'policy'
    return None, 'provider default'


# --- Adapters ----------------------------------------------------------------
# An adapter is read(route, timeout) -> dict and switch(route, model, timeout)
# -> dict, registered per transport. A missing piece raises Unsupported.

def agy_read(route, timeout):
    return agy_live.read_model(route['session'], timeout=timeout)


def agy_switch(route, model, timeout):
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


def claude_host(sid, sessions_dir=None):
    """The host that owns a live Claude session, from its registry record."""
    row = sessions.resolve_claude_peer(sid, sessions_dir)
    try:
        record = json.loads(Path(row['registry']).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        record = {}
    return record.get('entrypoint') or 'unknown'


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
CLAUDE_HOST_GAP = {
    'claude-vscode': CLAUDE_IDE_GAP,
    'claude-desktop': ('Claude desktop sessions are switched by the app (ccd_session_mgmt set_session_model); '
                       'that adapter is not in this bridge yet.'),
}


def claude_switch(route, model, timeout):
    host = claude_host(route['session'])
    raise Unsupported(f'claude ({host}): ' + CLAUDE_HOST_GAP.get(
        host, 'no bridge-reachable model switch is known for this Claude host.'))


ADAPTERS = {
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


def switch(route, model, policy, timeout=30):
    provider = PROVIDER.get(route.get('transport'), route.get('transport'))
    run = adapter(route, 'switch')
    names = adapter_names(route, model, timeout)
    require_allowed(policy, provider, *names)
    return {'provider': provider, **run(route, model, timeout)}


def adapter_names(route, model, timeout):
    found = ADAPTERS.get(route.get('transport'), {}).get('names')
    return found(route, model, min(timeout, 10)) if found else [model]
