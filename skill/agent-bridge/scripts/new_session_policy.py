"""Owner-edited policy and model defaults for new provider sessions.

Pure policy helpers: this module never contacts a provider or writes policy.
"""
import json
import os
from pathlib import Path

POLICY_FILE = 'policy.json'
CODEX_DESKTOP_WAKE_PERMISSIONS = 'codex_desktop_wake_permissions'
CODEX_SELF_ARCHIVE = 'codex_self_archive'
# Seed from the owner's rule of 2026-10-23; applies only while no policy file exists.
SEED_DENY = {'codex': ['gpt-6-astra']}
DEFAULT_MODELS = {'codex': 'gpt-6.2-sol'}


class CodexDesktopWakePolicyError(ValueError):
    def __init__(self):
        super().__init__(f'In owner policy.json, "{CODEX_DESKTOP_WAKE_PERMISSIONS}" must be omitted '
                         'or set to "workspace-write"')


def load_policy(state_dir):
    path = Path(state_dir).expanduser() / POLICY_FILE
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {'path': str(path), 'exists': False, 'defaults': {}, 'deny': SEED_DENY, 'command_post': {}}
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
    post = data.get('command_post', {})
    if not isinstance(post, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                             for k, v in post.items()):
        raise ValueError(f'{path}: "command_post" must map a provider to a registered route name')
    policy = {'path': str(path), 'exists': True, 'defaults': defaults, 'deny': deny, 'command_post': post}
    if CODEX_DESKTOP_WAKE_PERMISSIONS in data:
        if data[CODEX_DESKTOP_WAKE_PERMISSIONS] != 'workspace-write':
            raise CodexDesktopWakePolicyError()
        policy[CODEX_DESKTOP_WAKE_PERMISSIONS] = data[CODEX_DESKTOP_WAKE_PERMISSIONS]
    if CODEX_SELF_ARCHIVE in data:
        if type(data[CODEX_SELF_ARCHIVE]) is not bool:
            raise ValueError(f'In owner policy.json, "{CODEX_SELF_ARCHIVE}" must be a boolean')
        policy[CODEX_SELF_ARCHIVE] = data[CODEX_SELF_ARCHIVE]
    return policy


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

    Codex falls back to Sol; other providers keep their own default.
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
    if provider in DEFAULT_MODELS:
        return DEFAULT_MODELS[provider], 'default'
    return None, 'provider default'


