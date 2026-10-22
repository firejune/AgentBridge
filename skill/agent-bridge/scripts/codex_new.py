"""Create a persistent thread with the public app-server API, then adopt it in Desktop.

The creator is an ordinary, short-lived app-server client. Only exact Desktop
ownership and model readback admit a session to the bridge; no composer, native
pipe authorization workaround, synthetic user prompt, or creation replay.
"""
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import codex_live

FULL_ACCESS_PROFILE = ':danger-full-access'
# New threads request this model when --model is omitted, so they never
# inherit the Desktop's own default. Override per machine with MODEL_ENV.
DEFAULT_MODEL = 'gpt-6.1-sol'
MODEL_ENV = 'AGENT_BRIDGE_CODEX_MODEL'


class CreationError(ValueError):
    def __init__(self, reason, uncertain=False, session=None, adopted=False, stage=None):
        super().__init__(reason)
        self.uncertain = uncertain
        self.session = session
        self.created = session is not None
        self.adopted = adopted
        self.stage = stage
        self.cancelled = False


def adoption_url(sid):
    return 'codex://threads/' + str(uuid.UUID(sid))


def open_link(url, timeout=10):
    if not shutil.which('open'):
        raise CreationError('Adopting a Codex thread in Desktop needs the macOS `open` command')
    result = subprocess.run(['open', url], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=timeout)
    if result.returncode:
        raise CreationError('The exact Codex thread link was not opened')


def require_thread(value, sid, cwd, title=None):
    if not isinstance(value, dict) or value.get('id') != sid or value.get('ephemeral') is not False:
        raise CreationError('Codex returned a different, missing, or ephemeral thread')
    actual = value.get('cwd')
    if not isinstance(actual, str) or Path(actual).resolve() != cwd:
        raise CreationError('Codex thread cwd differs from the requested workspace')
    if title is not None and value.get('name') != title:
        raise CreationError('Codex thread title differs from the requested --title')
    return value


def resolve_model(model=None, environ=None):
    """--model, then MODEL_ENV, then DEFAULT_MODEL; returns (model, source)."""
    if model is not None:
        return model, '--model'
    value = (os.environ if environ is None else environ).get(MODEL_ENV)
    if value is not None:
        if not value.strip():
            raise CreationError(f'{MODEL_ENV} is set but empty; unset it or pass --model')
        return value, MODEL_ENV
    return DEFAULT_MODEL, 'default'


def require_model(client, model, remaining=None, missing=None):
    cursor, seen = None, set()
    while True:
        if remaining is not None:
            client.timeout = remaining()
        params = {'limit':100,'includeHidden':True}
        if cursor is not None:
            params['cursor'] = cursor
        page = client.call('model/list', params)
        rows = page.get('data')
        if not isinstance(rows, list):
            raise CreationError('Codex model/list returned no model catalog')
        if any(isinstance(row, dict) and row.get('model') == model for row in rows):
            return
        cursor = page.get('nextCursor')
        if not cursor:
            raise CreationError(missing or 'Requested --model is not advertised by the installed Codex provider')
        if not isinstance(cursor, str) or cursor in seen or len(seen) >= 100:
            raise CreationError('Codex model/list returned an invalid or repeated cursor')
        seen.add(cursor)


def require_full_access_profile(client, cwd, remaining):
    cursor, seen = None, set()
    while True:
        client.timeout = remaining()
        params = {'cwd':str(cwd),'limit':100}
        if cursor is not None:
            params['cursor'] = cursor
        page = client.call('permissionProfile/list', params)
        rows = page.get('data')
        if not isinstance(rows, list):
            raise CreationError('Codex permissionProfile/list returned no profile catalog')
        for row in rows:
            if isinstance(row, dict) and row.get('id') == FULL_ACCESS_PROFILE:
                if row.get('allowed') is True:
                    return
                raise CreationError('Installed Codex requirements refuse the Full Access profile')
        cursor = page.get('nextCursor')
        if not cursor:
            raise CreationError('Installed Codex does not advertise the Full Access profile')
        if not isinstance(cursor, str) or cursor in seen or len(seen) >= 100:
            raise CreationError('Codex permissionProfile/list returned an invalid or repeated cursor')
        seen.add(cursor)


def has_full_access(settings, sandbox_key='sandboxPolicy'):
    if not isinstance(settings, dict):
        return False
    profile, sandbox = settings.get('activePermissionProfile'), settings.get(sandbox_key)
    return (isinstance(profile, dict) and profile.get('id') == FULL_ACCESS_PROFILE
            and settings.get('approvalPolicy') == 'never'
            and isinstance(sandbox, dict) and sandbox.get('type') == 'dangerFullAccess')


def permission_settings(client, sid, owner):
    # latestThreadSettings are the app's confirmed subsequent-turn inputs;
    # currentPermissions may still describe the last completed turn.
    state = client.snapshot(sid, owner)
    values = state.get('latestThreadSettings')
    if not isinstance(values, dict):
        raise CreationError('Exact Codex Desktop owner returned no next-turn permission settings')
    return {key:values.get(key) for key in
            ('activePermissionProfile','approvalPolicy','sandboxPolicy')}


def owner_settings(client, sid, owner):
    response = client.call('thread-follower-read-model-settings', {'conversationId':sid}, 1, owner)
    result = response.get('result')
    settings = result.get('settings') if isinstance(result, dict) else None
    if (response.get('resultType') != 'success' or not isinstance(settings, dict)
            or not isinstance(settings.get('model'), str) or not settings['model']
            or settings.get('resumeState') != 'resumed'):
        raise CreationError('Exact Codex Desktop owner did not confirm resumed model settings')
    return settings


def open_codex(cwd, title=None, model=None, timeout=180, opener=None, client_factory=None,
               owner_factory=None, poll=0.5):
    import sessions
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise CreationError('Timeout must be between 0 and 3600 seconds')
    if not cwd or not str(cwd).strip():
        raise CreationError('Workspace --cwd is required to create a Codex desktop session')
    folder = Path(cwd).expanduser().resolve()
    if not folder.is_dir():
        raise CreationError(f'Workspace directory does not exist: {folder}')
    for label, value in (('--title',title),('--model',model)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise CreationError(f'{label} must be a nonempty string')
    model, source = resolve_model(model)
    missing = None if source == '--model' else (
        f'Default Codex model {model!r} (from {source}) is not advertised by the installed '
        'Codex provider; pass --model with an advertised model')
    if opener is None and not shutil.which('open'):
        raise CreationError('Adopting a Codex thread in Desktop needs the macOS `open` command')
    factory = client_factory or sessions.CodexClient
    owners = owner_factory or codex_live.Client
    deadline = time.monotonic() + timeout
    sid, adopted, attempted, stage = None, False, False, 'preflight'

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise CreationError('Codex session creation/adoption timed out; use the returned UUID to recover')
        return min(left, 10)

    try:
        with factory(mode='saved',timeout=remaining(),experimental=True) as creator:
            require_full_access_profile(creator, folder, remaining)
            require_model(creator, model, remaining, missing)
            params = {'cwd':str(folder),'ephemeral':False,'allowProviderModelFallback':False,
                      'permissions':FULL_ACCESS_PROFILE,'approvalPolicy':'never','model':model}
            stage, attempted = 'create', True
            creator.timeout = remaining()
            created = creator.call('thread/start', params)
            thread = created.get('thread')
            if not isinstance(thread, dict):
                raise CreationError('Codex thread/start returned no thread')
            sid = str(uuid.UUID(thread['id']))
            require_thread(thread, sid, folder)
            if created.get('model') != model:
                raise CreationError('Codex creator substituted the requested --model')
            if not has_full_access(created, 'sandbox'):
                raise CreationError('Codex creator did not confirm Full Access with approvalPolicy never')
            effective = {key:created.get(key) for key in
                         ('approvalPolicy','approvalsReviewer','sandbox','activePermissionProfile')}
            if title is not None:
                stage = 'title'
                creator.timeout = remaining()
                creator.call('thread/name/set', {'threadId':sid,'name':title})
        # Closing the creator is required before Desktop takes over. A fresh,
        # metadata-only client proves this is a persisted thread, not that
        # short-lived server's in-memory session.
        stage = 'persistence'
        with factory(mode='saved',timeout=remaining()) as reader:
            require_thread(reader.call('thread/read', {'threadId':sid,'includeTurns':False}).get('thread'),
                           sid, folder, title)
        stage = 'adoption'
        url = adoption_url(sid)
        if opener is None:
            open_link(url, remaining())
        else:
            opener(url)
        while True:
            remaining()
            try:
                client = owners(timeout=min(remaining(), 2))
                with client:
                    owner = client.owner(sid)
                    adopted = True
                    stage = 'model'
                    settings = owner_settings(client, sid, owner)
                    stage = 'permissions'
                    permissions = permission_settings(client, sid, owner)
                    updates = {}
                    if not has_full_access(permissions):
                        updates.update(permissions=FULL_ACCESS_PROFILE, approvalPolicy='never')
                    if settings['model'] != model:
                        updates['model'] = model
                    if updates:
                        # Change only this newly created thread, never global
                        # config or the permissions of an existing session.
                        response = client.call('thread-follower-update-thread-settings',
                            {'conversationId':sid,'threadSettings':updates}, 2, owner, mutation=True)
                        result = response.get('result')
                        if (response.get('resultType') != 'success' or not isinstance(result, dict)
                                or result.get('applied') is not True):
                            raise CreationError('Exact Codex Desktop owner did not confirm new-thread settings update')
                        settings = owner_settings(client, sid, owner)
                        permissions = permission_settings(client, sid, owner)
                    if settings['model'] != model:
                        raise CreationError('Exact Codex Desktop owner substituted the requested --model')
                    if not has_full_access(permissions):
                        raise CreationError('Exact Codex Desktop owner did not confirm Full Access with approvalPolicy never')
                    break
            except (codex_live.DeliveryError, OSError):
                if adopted:
                    raise  # Never replay a possibly sent settings mutation.
                time.sleep(min(poll, max(0, deadline-time.monotonic())))
        stage = 'verification'
        with factory(mode='saved',timeout=remaining()) as reader:
            stored = require_thread(reader.call('thread/read', {'threadId':sid,'includeTurns':False}).get('thread'),
                                    sid, folder, title)
        row = sessions.codex_row(stored, 'saved')
        return {'created':True,'adopted':True,'session':row,'owner':owner,'url':url,
                'model':settings['model'],'model_source':source,'creator_settings':effective,
                'permission_settings':permissions,'permission_settings_source':'Desktop owner latestThreadSettings',
                'warning':None}
    except BaseException as error:
        # Preserve recovery identity even when title, adoption, model update or
        # registration cannot be confirmed. No automatic second thread/start.
        if not isinstance(error, (Exception, KeyboardInterrupt)):
            raise
        message = str(error) if isinstance(error, CreationError) else 'Codex creation/adoption failed; do not create another thread automatically'
        failure = CreationError(message, attempted, sid, adopted, stage)
        failure.cancelled = isinstance(error, KeyboardInterrupt)
        raise failure from None


class ModelSwitchError(ValueError):
    def __init__(self, reason, uncertain=False):
        super().__init__(reason)
        self.uncertain = uncertain


def set_model(sid, model, timeout=10, client_factory=None, owner_factory=None):
    """Change one existing thread's model for its next turns.

    Sends only {model} through the exact Desktop owner, never permissions or
    any other field, and never interrupts an active turn. One attempt: a
    possibly sent mutation is never replayed.
    """
    import sessions
    try:
        sid = str(uuid.UUID(str(sid)))
    except ValueError:
        raise ModelSwitchError('SESSION_ID must be an exact Codex thread UUID') from None
    if not isinstance(model, str) or not model.strip():
        raise ModelSwitchError('--model must be a nonempty string')
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ModelSwitchError('Timeout must be between 0 and 3600 seconds')
    factory = client_factory or sessions.CodexClient
    owners = owner_factory or codex_live.Client
    client = None
    try:
        with factory(mode='saved',timeout=timeout,experimental=True) as catalog:
            require_model(catalog, model)
        client = owners(timeout=timeout)
        with client:
            owner = client.owner(sid)
            previous = owner_settings(client, sid, owner)['model']
            if previous != model:
                response = client.call('thread-follower-update-thread-settings',
                    {'conversationId':sid,'threadSettings':{'model':model}}, 2, owner, mutation=True)
                result = response.get('result')
                if (response.get('resultType') != 'success' or not isinstance(result, dict)
                        or result.get('applied') is not True):
                    raise ModelSwitchError('Exact Codex Desktop owner did not confirm the model update', True)
            current = owner_settings(client, sid, owner)['model']
            if current != model:
                raise ModelSwitchError(f'Exact Codex Desktop owner reads back model {current!r}, not {model!r}',
                                       client.mutation_sent)
        return {'session':sid,'owner':owner,'model':current,'previous_model':previous,
                'changed':previous != model,'applies':'next turn; an active turn is not interrupted'}
    except ModelSwitchError:
        raise
    except (CreationError, codex_live.DeliveryError, sessions.DiscoveryError, OSError) as error:
        # Never replay: a possibly sent mutation is reported uncertain instead.
        sent = client is not None and client.mutation_sent
        raise ModelSwitchError(str(error) if isinstance(error, (CreationError, codex_live.DeliveryError))
                               else 'Codex model switch transport failed', sent) from None
