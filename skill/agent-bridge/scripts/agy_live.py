"""Discover and push peer messages into a live Antigravity (AGY) desktop session.

Uses the local Antigravity language_server ConnectRPC endpoint (SendAgentMessage).
Never spawns a separate `agy -p` CLI process, never guesses another session, and
never logs or persists the language_server CSRF token.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SERVICE = '/exa.language_server_pb.LanguageServerService'
NEXT_INVOCATION = 'MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION'
WAKE_RETRY_DELAY = 0.05
BENIGN_WAKE_ERRORS = (
    'cannot queue empty user input step',
    'executor has not processed the previous input yet',
    'unimplemented',
)
CSRF_RE = re.compile(r'--csrf_token(?:=|\s+)([0-9a-fA-F-]+)')
APP_DIR_RE = re.compile(r'--app_data_dir(?:=|\s+)(antigravity(?:-ide)?)(?:\s|$)')
LSP_FLAG_RE = re.compile(r'(?:^|\s)--enable_lsp(?:\s|$)')
HUB_FLAG_RE = re.compile(r'(?:^|\s)--hub(?:\s|$)')
HUB_PORT_RE = re.compile(r'--hub-port(?:=|\s+)(\d+)(?:\s|$)')
ADD_DIR_RE = re.compile(r'--add-dir(?:=|\s+)(\S+)')
SUBCLIENT_IDE_RE = re.compile(r'--subclient_type(?:=|\s+)ide(?:\s|$)')
AGY_BIN_RE = re.compile(r'(?:^|[\s/])agy(?:\s|$)')
PORT_RE = re.compile(r'(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)\s+\(LISTEN\)')
ORIGIN_CHARS = re.compile(r'[^A-Za-z0-9%:_/.\-]')


class DeliveryError(ValueError):
    def __init__(self, reason, uncertain=False):
        super().__init__(reason)
        self.uncertain = uncertain


def observed_now():
    return datetime.now(timezone.utc).isoformat()


def uri_to_path(uri):
    if not isinstance(uri, str) or not uri.startswith('file://'):
        return None
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != 'file' or not parsed.path:
        return None
    return str(Path(urllib.parse.unquote(parsed.path)).resolve())


def extract_cwd(workspaces):
    if not isinstance(workspaces, list):
        return None
    for item in workspaces:
        if isinstance(item, dict):
            path = uri_to_path(item.get('workspaceFolderAbsoluteUri'))
            if path:
                return path
    return None


def normalize_status(raw):
    if not isinstance(raw, str) or not raw:
        return 'unknown'
    mapping = {
        'CASCADE_RUN_STATUS_RUNNING': 'running',
        'CASCADE_RUN_STATUS_IDLE': 'idle',
        'CASCADE_RUN_STATUS_CANCELLED': 'cancelled',
        'CASCADE_RUN_STATUS_FAILED': 'failed',
    }
    return mapping.get(raw, raw)


def rpc_call(url, csrf, method, payload, timeout=5):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        f"{url.rstrip('/')}{SERVICE}/{method}",
        data=body,
        headers={
            'Content-Type': 'application/json',
            'x-codeium-csrf-token': csrf,
        },
        method='POST',
    )
    context = ssl._create_unverified_context() if url.startswith('https://') else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            raw = resp.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        detail = ''
        try:
            err_json = json.loads(error.read(65536).decode('utf-8', errors='replace'))
            if isinstance(err_json, dict) and isinstance(err_json.get('message'), str):
                detail = f": {err_json['message']}"
        except Exception:
            pass
        raise DeliveryError(f'Antigravity {method} HTTP {error.code}{detail}') from None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, 'reason', error)
        raise DeliveryError(f'Antigravity {method} connection failed: {reason}') from None
    try:
        data = json.loads(raw.decode('utf-8')) if raw else {}
    except (ValueError, UnicodeError):
        raise DeliveryError(f'Antigravity {method} returned invalid JSON') from None
    if not isinstance(data, dict):
        raise DeliveryError(f'Antigravity {method} returned a non-object response')
    if 'code' in data and 'message' in data and method != 'GetAllCascadeTrajectories':
        raise DeliveryError(f"Antigravity {method}: {data['message']}")
    return data


def trajectory_summaries(data):
    """The conversation map, or None when the reply is not one.

    ProtoJSON omits an empty map, so a server with no conversations yet (a
    fresh Desktop hub) answers {}: that is an empty registry, not a failure.
    """
    if 'code' in data and 'message' in data:
        return None  # an error envelope; rpc_call lets this method's through
    if 'trajectorySummaries' not in data:
        return {}
    summaries = data['trajectorySummaries']
    return summaries if isinstance(summaries, dict) else None


def validate_override_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or not parsed.port:
        raise DeliveryError('AGENT_BRIDGE_AGY_URL must be a loopback http/https URL with an explicit port')
    return f'{parsed.scheme}://{parsed.hostname}:{parsed.port}'


def server_host(server):
    if isinstance(server, dict):
        host = server.get('host')
        if host in ('desktop', 'ide'):
            return host
        if server.get('app_dir') == 'antigravity-ide':
            return 'ide'
    return 'desktop'


def _parse_server_command(cmd):
    if LSP_FLAG_RE.search(cmd):
        return None
    is_lang_server = 'language_server' in cmd
    is_agy_hub = bool(HUB_FLAG_RE.search(cmd) and AGY_BIN_RE.search(cmd))
    if not is_lang_server and not is_agy_hub:
        return None
    app_match = APP_DIR_RE.search(cmd)
    csrf_match = CSRF_RE.search(cmd)
    if not app_match or not csrf_match:
        return None
    app_dir = app_match.group(1)
    csrf = csrf_match.group(1)
    host = 'ide' if (is_agy_hub or app_dir == 'antigravity-ide' or SUBCLIENT_IDE_RE.search(cmd)) else 'desktop'
    port_match = HUB_PORT_RE.search(cmd)
    hub_port = int(port_match.group(1)) if port_match else None
    add_dirs = []
    for match in ADD_DIR_RE.finditer(cmd):
        raw = match.group(1).strip()
        if raw:
            try:
                resolved = str(Path(raw).expanduser().resolve())
            except (OSError, ValueError):
                resolved = raw
            if resolved not in add_dirs:
                add_dirs.append(resolved)
    return {
        'app_dir': app_dir,
        'csrf': csrf,
        'host': host,
        'is_agy_hub': is_agy_hub,
        'hub_port': hub_port,
        'add_dirs': tuple(add_dirs),
    }


def _resolve_lsof():
    if shutil.which('lsof'):
        return 'lsof'
    for candidate in ('/usr/sbin/lsof', '/usr/bin/lsof'):
        if os.access(candidate, os.X_OK):
            return candidate
    return 'lsof'


def _connect_candidate(pid, app_dir, csrf, timeout=5, host=None, add_dirs=(), hub_port=None, ppid=None, orphaned=False):
    lsof_out = ''
    try:
        lsof_out = subprocess.check_output(
            [_resolve_lsof(), '-Pan', '-p', str(pid), '-iTCP', '-sTCP:LISTEN'],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        if not hub_port:
            raise DeliveryError(f'Failed to inspect listening ports for Antigravity pid {pid}: {error}') from None

    ports = []
    for line in lsof_out.splitlines():
        match = PORT_RE.search(line)
        if match:
            port = int(match.group(1))
            if port not in ports:
                ports.append(port)
    if hub_port and hub_port not in ports:
        ports.append(hub_port)

    if not ports:
        raise DeliveryError(f'Antigravity {app_dir} language_server (pid {pid}) has no loopback listening port')

    resolved_host = host or ('ide' if app_dir == 'antigravity-ide' else 'desktop')
    last_error = None
    for port in ports:
        schemes = ('http', 'https') if port == hub_port else ('https', 'http')
        for scheme in schemes:
            url = f'{scheme}://127.0.0.1:{port}'
            try:
                data = rpc_call(url, csrf, 'GetAllCascadeTrajectories', {}, timeout=timeout)
                summaries = trajectory_summaries(data)
                if summaries is not None:
                    return {
                        'pid': pid,
                        'ppid': ppid,
                        'orphaned': bool(orphaned),
                        'app_dir': app_dir,
                        'host': resolved_host,
                        'add_dirs': tuple(add_dirs or ()),
                        'url': url,
                        'csrf': csrf,
                        'summaries': summaries,
                    }
            except DeliveryError as error:
                last_error = error
    raise DeliveryError(f'Unable to connect to Antigravity {app_dir} language_server (pid {pid}): {last_error}')


def _parse_ps_line(line):
    parts4 = line.strip().split(None, 3)
    if len(parts4) == 4:
        try:
            return int(parts4[0]), int(parts4[1]), int(parts4[2]), parts4[3]
        except ValueError:
            pass
    parts3 = line.strip().split(None, 2)
    if len(parts3) < 3:
        return None
    try:
        return int(parts3[0]), None, int(parts3[1]), parts3[2]
    except ValueError:
        return None


# TODO(remove-when-upstream-fixed): Antigravity IDE's `extensions/agy` spawns
# `~/.gemini/bin/agy --hub` with stdin bound to `/dev/null` and without
# `--parent_pipe_path`. When the Extension Host (`Antigravity IDE Helper (Plugin)`)
# exits before its async `deactivate()` hook finishes, `agy --hub` is reparented
# to `launchd` (PPID == 1) and stays alive indefinitely. Remove this automatic
# orphan reaper once upstream Antigravity binds `agy --hub` to its parent lifetime.
def _terminate_orphaned_server(pid):
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _has_running_trajectory(summaries):
    if not isinstance(summaries, dict):
        return False
    return any(
        isinstance(info, dict) and normalize_status(info.get('status')) == 'running'
        for info in summaries.values()
    )


def _drop_orphans_if_live_present(entries):
    live = [entry for entry in entries if not entry[1].get('orphaned')]
    if live:
        for pid, info in entries:
            if info.get('orphaned'):
                _terminate_orphaned_server(pid)
        return live
    return entries


def discover_servers(timeout=5):
    override_url = os.environ.get('AGENT_BRIDGE_AGY_URL')
    override_csrf = os.environ.get('AGENT_BRIDGE_AGY_CSRF')
    if override_url:
        if not override_csrf:
            raise DeliveryError('AGENT_BRIDGE_AGY_CSRF is required when AGENT_BRIDGE_AGY_URL is set')
        url = validate_override_url(override_url)
        data = rpc_call(url, override_csrf, 'GetAllCascadeTrajectories', {}, timeout=timeout)
        summaries = trajectory_summaries(data)
        if summaries is None:
            raise DeliveryError('Antigravity server did not return trajectorySummaries')
        return [{
            'pid': os.getpid(),
            'ppid': os.getppid(),
            'orphaned': False,
            'app_dir': 'antigravity',
            'host': 'desktop',
            'add_dirs': (),
            'url': url,
            'csrf': override_csrf,
            'summaries': summaries,
        }]

    uid = os.getuid()
    try:
        ps_out = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,uid=,command='], text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise DeliveryError(f'Failed to inspect local processes for Antigravity: {error}') from None

    desktop_items = []
    legacy_ide_items = []
    hub_ide_items = []
    for line in ps_out.splitlines():
        row = _parse_ps_line(line)
        if not row:
            continue
        pid, ppid, proc_uid, cmd = row
        if proc_uid != uid:
            continue
        parsed = _parse_server_command(cmd)
        if not parsed:
            continue
        parsed['ppid'] = ppid
        parsed['orphaned'] = (ppid is not None and ppid <= 1)
        entry = (pid, parsed)
        if parsed['host'] == 'desktop':
            desktop_items.append(entry)
        elif parsed['is_agy_hub']:
            hub_ide_items.append(entry)
        else:
            legacy_ide_items.append(entry)

    if not desktop_items and not legacy_ide_items and not hub_ide_items:
        raise DeliveryError('Antigravity language_server (desktop or IDE) is not running')

    desktop_items = _drop_orphans_if_live_present(desktop_items)
    legacy_ide_items = _drop_orphans_if_live_present(legacy_ide_items)

    if len(desktop_items) > 1:
        raise DeliveryError(f'{len(desktop_items)} Antigravity antigravity language_server processes found; not choosing one')
    if len(legacy_ide_items) > 1:
        raise DeliveryError(f'{len(legacy_ide_items)} Antigravity antigravity-ide language_server processes found; not choosing one')

    servers = []
    last_error = None
    reaped_orphan = False
    for pid, info in (*desktop_items, *legacy_ide_items, *hub_ide_items):
        try:
            candidate = _connect_candidate(
                pid,
                info['app_dir'],
                info['csrf'],
                timeout=timeout,
                host=info['host'],
                add_dirs=info['add_dirs'],
                hub_port=info['hub_port'],
                ppid=info.get('ppid'),
                orphaned=info.get('orphaned', False),
            )
        except DeliveryError as error:
            if info.get('orphaned'):
                _terminate_orphaned_server(pid)
                reaped_orphan = True
            else:
                last_error = error
            continue
        if candidate.get('orphaned') and not _has_running_trajectory(candidate.get('summaries')):
            _terminate_orphaned_server(pid)
            reaped_orphan = True
            continue
        servers.append(candidate)

    if not servers:
        if reaped_orphan and last_error is None:
            raise DeliveryError('Antigravity language_server (desktop or IDE) is not running')
        raise last_error or DeliveryError('Unable to connect to any Antigravity language_server')
    return servers


def discover_server(timeout=5):
    servers = discover_servers(timeout=timeout)
    for server in servers:
        if not server.get('orphaned'):
            return server
    return servers[0]


HOST_APP_DIRS = {'desktop': 'antigravity', 'ide': 'antigravity-ide'}


def _git_main_worktree(cwd):
    if not cwd:
        return None
    try:
        target = Path(cwd).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return None
    for candidate in (target, *target.parents):
        git_marker = candidate / '.git'
        try:
            if not git_marker.is_file():
                continue
            first_line = git_marker.read_text(encoding='utf-8', errors='replace').splitlines()[0].strip()
        except (OSError, IndexError):
            continue
        if not first_line.startswith('gitdir:'):
            continue
        raw_gitdir = first_line[len('gitdir:'):].strip()
        if not raw_gitdir:
            continue
        try:
            gitdir = (candidate / raw_gitdir).resolve()
        except (OSError, ValueError):
            continue
        if gitdir.parent.name == 'worktrees' and gitdir.parent.parent.name == '.git':
            return gitdir.parent.parent.parent.resolve()
    return None


def _server_matches_cwd(server, cwd):
    if not cwd or not isinstance(server, dict):
        return False
    try:
        target = Path(cwd).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return False
    main_repo = _git_main_worktree(target)
    for raw in server.get('add_dirs') or ():
        try:
            folder = Path(raw).expanduser().resolve()
        except (OSError, TypeError, ValueError):
            continue
        if target == folder or folder in target.parents:
            return True
        if main_repo is not None and (main_repo == folder or folder in main_repo.parents):
            return True
    return False


def discover_host_server(host='desktop', timeout=5, cwd=None):
    if host not in HOST_APP_DIRS:
        raise DeliveryError(f'Unknown Antigravity host {host!r}; use desktop or ide')
    servers = [s for s in discover_servers(timeout=timeout) if not s.get('orphaned')]
    candidates = [s for s in servers if server_host(s) == host]
    if not candidates and host == 'desktop':
        # Unified agy --hub servers run with --app_data_dir=antigravity and
        # serve both Agent Manager (desktop) and IDE when no separate desktop
        # language_server is running.
        candidates = [s for s in servers if s.get('app_dir') == 'antigravity']
    if not candidates:
        label = 'desktop' if host == 'desktop' else 'IDE'
        raise DeliveryError(f'Antigravity {label} language_server is not running')
    if cwd and len(candidates) > 1:
        target_cwd = str(Path(cwd).expanduser().resolve())
        exact = [s for s in candidates if target_cwd in (s.get('add_dirs') or ())]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise DeliveryError(f'{len(exact)} Antigravity IDE servers match workspace {target_cwd}; not choosing one')
        matched = [s for s in candidates if _server_matches_cwd(s, target_cwd)]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            raise DeliveryError(f'{len(matched)} Antigravity IDE servers match workspace {target_cwd}; not choosing one')
        main_repo = _git_main_worktree(target_cwd)
        main_repo_str = str(main_repo) if main_repo is not None else None
        in_summary = [
            s for s in candidates
            if any(
                _summary_cwd(item) == target_cwd
                or (main_repo_str and (
                    _summary_cwd(item) == main_repo_str
                    or str(_git_main_worktree(_summary_cwd(item)) or '') == main_repo_str
                ))
                for item in (s.get('summaries') or {}).values()
                if isinstance(item, dict)
            )
        ]
        if len(in_summary) == 1:
            return in_summary[0]
    return candidates[0]


def discover_desktop_server(timeout=5):
    return discover_host_server('desktop', timeout=timeout)


def gemini_projects_dir():
    base = os.environ.get('GEMINI_CONFIG_DIR')
    if base:
        candidate = Path(base).expanduser()
        if (candidate / 'projects').is_dir():
            return candidate / 'projects'
        return candidate / 'config' / 'projects'
    return Path.home() / '.gemini' / 'config' / 'projects'


def _resource_folder_path(res):
    if not isinstance(res, dict):
        return None
    uri = res.get('folderUri')
    if not uri and isinstance(res.get('gitFolder'), dict):
        uri = res['gitFolder'].get('folderUri')
    return uri_to_path(uri)


def _project_default_paths(record):
    if not isinstance(record, dict):
        return ()
    paths = []
    for raw_path in record.get('workspacePaths') or ():
        if isinstance(raw_path, str) and raw_path.strip():
            try:
                folder_path = (
                    uri_to_path(raw_path)
                    if raw_path.startswith('file://')
                    else str(Path(raw_path).expanduser().resolve())
                )
            except (OSError, ValueError):
                folder_path = None
            if folder_path and folder_path not in paths:
                paths.append(folder_path)
    resources = record.get('projectResources', {}).get('resources')
    if isinstance(resources, list):
        for res in resources:
            folder_path = _resource_folder_path(res)
            if folder_path and folder_path not in paths:
                paths.append(folder_path)
    return tuple(paths)


def _find_project_environment_id(record, target_path_str):
    if not isinstance(record, dict) or not target_path_str:
        return None
    envs = record.get('environments', {}).get('environments')
    if not isinstance(envs, list):
        return None
    for env in envs:
        if not isinstance(env, dict):
            continue
        env_id = env.get('id')
        if not isinstance(env_id, str) or not env_id:
            continue
        resources = env.get('resources', {}).get('resources')
        if not isinstance(resources, list):
            continue
        for res in resources:
            if _resource_folder_path(res) == target_path_str:
                return env_id
    return None


def _load_project_record(project_id, projects_dir=None):
    if not isinstance(project_id, str) or not project_id:
        return None
    root = Path(projects_dir) if projects_dir else gemini_projects_dir()
    if not root.is_dir():
        return None
    direct = root / f'{project_id}.json'
    candidates = [direct] if direct.is_file() else sorted(root.glob('*.json'))
    for path in candidates:
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get('id') == project_id:
            return record
    return None


IGNORED_PROJECT_IDS = ('outside-of-project', 'default-cli-project')


def find_desktop_project_id(server, target_cwd, projects_dir=None):
    resolved_target = str(Path(target_cwd).expanduser().resolve())
    main_repo = _git_main_worktree(resolved_target)
    resolved_root = str(main_repo) if main_repo is not None else resolved_target

    summary_root_match = None
    summaries = server.get('summaries') if isinstance(server, dict) else None
    if isinstance(summaries, dict):
        for item in summaries.values():
            if not isinstance(item, dict):
                continue
            meta = item.get('trajectoryMetadata')
            project_id = meta.get('projectId') if isinstance(meta, dict) else None
            if not isinstance(project_id, str) or not project_id or project_id in IGNORED_PROJECT_IDS:
                continue
            cwd = extract_cwd(item.get('workspaces')) or extract_cwd(meta.get('workspaces'))
            if not cwd:
                continue
            resolved_cwd = str(Path(cwd).resolve())
            if resolved_cwd == resolved_target:
                return project_id
            if summary_root_match is None:
                cwd_root = _git_main_worktree(resolved_cwd)
                if resolved_cwd == resolved_root or (cwd_root is not None and str(cwd_root) == resolved_root):
                    summary_root_match = project_id

    root = Path(projects_dir) if projects_dir else gemini_projects_dir()
    worktree_match = None
    if root.is_dir():
        for path in sorted(root.glob('*.json')):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            project_id = record.get('id')
            if not isinstance(project_id, str) or not project_id or project_id in IGNORED_PROJECT_IDS:
                continue
            default_paths = _project_default_paths(record)
            if resolved_target in default_paths or _find_project_environment_id(record, resolved_target):
                return project_id
            if worktree_match is None and resolved_root != resolved_target and resolved_root in default_paths:
                worktree_match = project_id
    return worktree_match or summary_root_match


def _create_desktop_project(server, project_root, timeout=5):
    project_root = Path(project_root).expanduser().resolve()
    if not (project_root / '.git').exists():
        return None
    project_id = str(uuid.uuid4())
    project_name = project_root.name or str(project_root)
    try:
        rpc_call(
            server['url'],
            server['csrf'],
            'CreateProject',
            {
                'project': {
                    'id': project_id,
                    'name': project_name,
                    'projectResources': {
                        'resources': [
                            {
                                'gitFolder': {
                                    'folderUri': project_root.as_uri(),
                                },
                            },
                        ],
                    },
                    'isWorkspaceOnly': False,
                    'settings': {},
                },
            },
            timeout=timeout,
        )
        return project_id
    except DeliveryError:
        return None


def _resolve_project_env_config(server, project_id, target_path, projects_dir=None, timeout=5):
    if not isinstance(project_id, str) or not project_id:
        return None
    target_path = Path(target_path).expanduser().resolve()
    target_str = str(target_path)
    main_repo = _git_main_worktree(target_path)
    project_root = main_repo if main_repo is not None else target_path
    record = _load_project_record(project_id, projects_dir=projects_dir)
    default_paths = _project_default_paths(record) if record else ()

    if target_str in default_paths or (not default_paths and target_path == project_root):
        return {
            'projectId': project_id,
            'defaultProjectEnvironment': {},
        }

    env_id = _find_project_environment_id(record, target_str) if record else None
    if not env_id:
        env_id = str(uuid.uuid4())
        try:
            rpc_call(
                server['url'],
                server['csrf'],
                'AddEnvironmentToProject',
                {
                    'projectId': project_id,
                    'environment': {
                        'id': env_id,
                        'name': target_path.name or target_str,
                        'resources': {
                            'resources': [
                                {'folderUri': target_path.as_uri()},
                            ],
                        },
                    },
                },
                timeout=timeout,
            )
        except DeliveryError:
            return None
    return {
        'projectId': project_id,
        'environmentId': env_id,
    }


def build_default_agent_spec(model_enum):
    cascade_config = {
        'plannerConfig': {
            'planModel': model_enum,
            'conversational': {},
            'supportsLatexRendering': True,
        },
        'executorConfig': {
            'useCoreDirect': True,
        },
    }
    return {
        'builtinAgent': {
            'defaultAgent': {
                'isInteractive': True,
                'surface': 'AGENT_SURFACE_HUB',
            },
            'model': model_enum,
            'cascadeConfig': cascade_config,
        },
        'cascadeConfig': cascade_config,
    }


def resolve_model(server, requested_model=None, timeout=5):
    default_enum = 'MODEL_PLACEHOLDER_M418'
    configs = []
    try:
        data = rpc_call(server['url'], server['csrf'], 'GetCascadeModelConfigData', {}, timeout=timeout)
        if isinstance(data.get('clientModelConfigs'), list):
            configs = [c for c in data['clientModelConfigs'] if isinstance(c, dict)]
        override_enum = (
            data.get('defaultOverrideModelConfig', {})
            .get('modelOrAlias', {})
            .get('model')
        )
        if isinstance(override_enum, str) and override_enum:
            default_enum = override_enum
        elif configs:
            first_enum = configs[0].get('modelOrAlias', {}).get('model')
            if isinstance(first_enum, str) and first_enum:
                default_enum = first_enum
    except DeliveryError as error:
        text = str(error).lower()
        if 'unimplemented' not in text and 'http 404' not in text:
            raise

    if not requested_model:
        for cfg in configs:
            if cfg.get('modelOrAlias', {}).get('model') == default_enum:
                return {
                    'model_enum': default_enum,
                    'model_id': cfg.get('modelId') or default_enum,
                    'label': cfg.get('label'),
                }
        return {'model_enum': default_enum, 'model_id': default_enum, 'label': None}

    needle = str(requested_model).strip()
    folded = needle.casefold()
    for cfg in configs:
        model_enum = cfg.get('modelOrAlias', {}).get('model')
        model_id = cfg.get('modelId')
        label = cfg.get('label')
        if not isinstance(model_enum, str) or not model_enum:
            continue
        if (
            model_enum.casefold() == folded
            or (isinstance(model_id, str) and model_id.casefold() == folded)
            or (isinstance(label, str) and label.casefold() == folded)
        ):
            return {
                'model_enum': model_enum,
                'model_id': model_id or model_enum,
                'label': label,
            }

    if needle.startswith('MODEL_'):
        return {'model_enum': needle, 'model_id': needle, 'label': None}

    available = [c.get('modelId') for c in configs if isinstance(c.get('modelId'), str)]
    suffix = f" (available: {', '.join(available)})" if available else ''
    raise DeliveryError(f'Unknown Antigravity model {needle!r}{suffix}')


def _summary_cwd(item, fallback_cwd=None):
    if not isinstance(item, dict):
        return fallback_cwd
    meta = item.get('trajectoryMetadata')
    return (
        extract_cwd(item.get('workspaces'))
        or extract_cwd(meta.get('workspaces') if isinstance(meta, dict) else None)
        or fallback_cwd
    )


def _workspace_affinity(server, cwd):
    if server_host(server) == 'ide':
        add_dirs = server.get('add_dirs') or ()
        if add_dirs:
            return 2 if _server_matches_cwd(server, cwd) else 0
    return 1


def _summary_rank(server, item, fallback_cwd=None):
    if not isinstance(item, dict):
        return (0, 0, -1, '', 0)
    is_running = 1 if normalize_status(item.get('status')) == 'running' else 0
    not_orphaned = 0 if (isinstance(server, dict) and server.get('orphaned')) else 1
    try:
        steps = int(item.get('stepCount') or 0)
    except (TypeError, ValueError):
        steps = 0
    modified = str(item.get('lastModifiedTime') or '')
    cwd = _summary_cwd(item, fallback_cwd)
    affinity = _workspace_affinity(server, cwd)
    return (is_running, not_orphaned, steps, modified, affinity)


def agy_row(sid, item, pid, fallback_cwd=None, app_dir='antigravity', host=None):
    sid = str(uuid.UUID(sid))
    raw_status = item.get('status', 'unknown') if isinstance(item, dict) else 'unknown'
    cwd = _summary_cwd(item, fallback_cwd)
    surface = 'IDE' if (host == 'ide' or app_dir == 'antigravity-ide') else 'desktop'
    annotations = item.get('annotations') if isinstance(item, dict) and isinstance(item.get('annotations'), dict) else {}
    title = (item.get('summary') if isinstance(item, dict) else None) or annotations.get('title') or None
    return {
        'provider': 'agy',
        'id': sid,
        'name': title,
        'cwd': cwd,
        'status': normalize_status(raw_status),
        'reported_status': raw_status,
        'runtime_observed': True,
        'pid': pid,
        'updated_at': item.get('lastModifiedTime') if isinstance(item, dict) else None,
        'observed_at': observed_now(),
        'scope': f'Antigravity {surface} live trajectory registry',
    }


def list_agy(cwd=None, search=None, limit=100, timeout=5):
    servers = discover_servers(timeout=timeout)
    target_cwd = str(Path(cwd).expanduser().resolve()) if cwd else None
    best_by_sid = {}
    for server in servers:
        for cid, item in server['summaries'].items():
            try:
                sid = str(uuid.UUID(cid))
            except ValueError:
                continue
            rank = _summary_rank(server, item)
            prev = best_by_sid.get(sid)
            if prev is None or rank > prev[0]:
                best_by_sid[sid] = (rank, server, item)
    combined = sorted(
        ((server, sid, item) for sid, (_, server, item) in best_by_sid.items()),
        key=lambda entry: (entry[2].get('lastModifiedTime') or '') if isinstance(entry[2], dict) else '',
        reverse=True,
    )
    rows = []
    for server, sid, item in combined:
        row = agy_row(
            sid,
            item,
            server['pid'],
            app_dir=server.get('app_dir', 'antigravity'),
            host=server.get('host'),
        )
        if not row['cwd']:
            continue
        if target_cwd and str(Path(row['cwd']).resolve()) != target_cwd:
            continue
        if search and search.casefold() not in (row['name'] or '').casefold():
            continue
        rows.append(row)
    return {
        'sessions': rows[:limit],
        'more_available': len(rows) > limit,
        'warning': None,
        'scope': 'live trajectories in Antigravity IDE and desktop language_server instances with a workspace',
    }


def resolve_agy(sid, timeout=5):
    sid = str(uuid.UUID(sid))
    servers = discover_servers(timeout=timeout)
    in_summaries = [
        (server, server['summaries'][sid])
        for server in servers
        if isinstance(server.get('summaries'), dict) and isinstance(server['summaries'].get(sid), dict)
    ]
    if in_summaries:
        running = [pair for pair in in_summaries if normalize_status(pair[1].get('status')) == 'running']
        if len(running) > 1:
            raise DeliveryError(f'Antigravity session {sid} matched multiple live servers')
        ranked = sorted(
            in_summaries,
            key=lambda pair: _summary_rank(pair[0], pair[1]),
            reverse=True,
        )
        if len(ranked) > 1 and _summary_rank(ranked[0][0], ranked[0][1]) == _summary_rank(ranked[1][0], ranked[1][1]):
            raise DeliveryError(f'Antigravity session {sid} matched multiple live servers')
        candidates = ranked[:1]
    else:
        candidates = [(server, None) for server in servers]

    matches = []
    last_error = None
    for server, summary_item in candidates:
        meta = None
        summary_cwd = _summary_cwd(summary_item)
        if not summary_cwd:
            try:
                meta_resp = rpc_call(
                    server['url'], server['csrf'], 'GetConversationMetadata',
                    {'conversationId': sid}, timeout=timeout,
                )
                meta = meta_resp.get('metadata')
                if not isinstance(meta, dict):
                    nested = meta_resp.get('response', {}).get('conversationMetadata', {})
                    meta = nested.get('metadata') if isinstance(nested, dict) else None
            except DeliveryError as error:
                last_error = error
                if not isinstance(summary_item, dict):
                    continue
        if not isinstance(meta, dict) and not isinstance(summary_item, dict) and not summary_cwd:
            continue
        meta_cwd = extract_cwd(meta.get('workspaces') if isinstance(meta, dict) else None)
        row = agy_row(
            sid,
            summary_item or {},
            server['pid'],
            fallback_cwd=summary_cwd or meta_cwd,
            app_dir=server.get('app_dir', 'antigravity'),
            host=server.get('host'),
        )
        if not row['cwd']:
            last_error = DeliveryError(f'Antigravity session {sid} has no workspace directory')
            continue
        matches.append((server, row))

    if not matches:
        raise last_error or DeliveryError(f'Antigravity session {sid} is not found in any live desktop or IDE server')
    if len(matches) > 1:
        ranked_matches = sorted(
            matches,
            key=lambda pair: _workspace_affinity(pair[0], pair[1]['cwd']),
            reverse=True,
        )
        top_affinity = _workspace_affinity(ranked_matches[0][0], ranked_matches[0][1]['cwd'])
        second_affinity = _workspace_affinity(ranked_matches[1][0], ranked_matches[1][1]['cwd'])
        if top_affinity > second_affinity:
            return ranked_matches[0]
        raise DeliveryError(f'Antigravity session {sid} matched multiple live servers')
    return matches[0]


def read_agy(sid, timeout=5):
    _, row = resolve_agy(sid, timeout=timeout)
    return {'session': row, 'warning': None}


def open_agy(cwd, title=None, model=None, timeout=5, host='desktop'):
    if not cwd or not str(cwd).strip():
        raise DeliveryError('Workspace --cwd is required to open an Antigravity session')
    target_path = Path(cwd).expanduser().resolve()
    if not target_path.is_dir():
        raise DeliveryError(f'Workspace directory does not exist: {target_path}')

    server = discover_host_server(host, timeout=timeout, cwd=str(target_path))
    model_info = resolve_model(server, model, timeout=timeout)
    # Unified agy --hub (app_dir == 'antigravity') and desktop servers group
    # conversations by projectId in the UI conversation stack; only legacy
    # antigravity-ide language_server instances skip project association.
    supports_projects = (host == 'desktop' or server.get('app_dir') == 'antigravity')
    project_id = find_desktop_project_id(server, str(target_path)) if supports_projects else None
    if supports_projects and not project_id:
        project_root = _git_main_worktree(target_path) or target_path
        project_id = _create_desktop_project(server, project_root, timeout=timeout)
    project_env_config = (
        _resolve_project_env_config(server, project_id, target_path, timeout=timeout)
        if project_id else None
    )
    if project_id and not project_env_config:
        project_id = None

    cid = str(uuid.uuid4())
    base_payload = {
        'cascadeId': cid,
        'source': 'CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT',
        'requestedModel': model_info['model_enum'],
        'customAgentSpec': build_default_agent_spec(model_info['model_enum']),
    }

    start_resp = None
    if project_env_config:
        try:
            start_resp = rpc_call(
                server['url'],
                server['csrf'],
                'StartCascade',
                {
                    **base_payload,
                    'projectEnvConfig': project_env_config,
                },
                timeout=timeout,
            )
        except DeliveryError:
            project_id = None

    if start_resp is None:
        start_resp = rpc_call(
            server['url'],
            server['csrf'],
            'StartCascade',
            {
                **base_payload,
                'workspaceUris': [target_path.as_uri()],
            },
            timeout=timeout,
        )

    actual_cid = str(uuid.UUID(start_resp.get('cascadeId') or cid))
    clean_title = (str(title).strip() if isinstance(title, str) and title.strip()
                   else (target_path.name or str(target_path)))
    annotations = {'lastUserViewTime': observed_now(), 'title': clean_title}
    try:
        rpc_call(
            server['url'],
            server['csrf'],
            'UpdateConversationAnnotations',
            {
                'cascadeId': actual_cid,
                'annotations': annotations,
                'mergeAnnotations': True,
            },
            timeout=timeout,
        )
    except DeliveryError as error:
        text = str(error).lower()
        if 'unimplemented' not in text and 'http 404' not in text:
            raise

    _, row = resolve_agy(actual_cid, timeout=timeout)
    if clean_title and not row.get('name'):
        row['name'] = clean_title
    return {
        'created': True,
        'session': row,
        'model': model_info['model_id'],
        'model_enum': model_info['model_enum'],
        'project_id': project_id,
        'host': host,
        'warning': None,
    }


# --- Model read/switch (issue #13) ------------------------------------------
# The Antigravity panel labels a conversation's model from its last generator's
# plannerConfig, and its model picker sends cascadeConfig.plannerConfig.
# requestedModel with the next SendUserCascadeMessage. The bridge reads and
# writes the same two places; it never invents a separate setting.
PLANNER_ONLY_FIELDS = ('modelName', 'planModel', 'requestedModel')


class ModelSwitchError(DeliveryError):
    pass


def model_catalog(server, timeout=5):
    data = rpc_call(server['url'], server['csrf'], 'GetCascadeModelConfigData', {}, timeout=timeout)
    rows = data.get('clientModelConfigs')
    return [c for c in rows if isinstance(c, dict)] if isinstance(rows, list) else []


def describe_model(catalog, model_enum):
    for cfg in catalog:
        if cfg.get('modelOrAlias', {}).get('model') == model_enum:
            return {'model': cfg.get('modelId') or model_enum, 'model_enum': model_enum, 'label': cfg.get('label')}
    return {'model': model_enum, 'model_enum': model_enum, 'label': None}


def last_generator(server, sid, steps, timeout=5):
    """The newest generator entry without fetching the whole history.

    generatorMetadataOffset skips that many entries, and there are never more
    generators than steps, so probe downward from stepCount with a doubling gap.
    Returns (entry or None, count).
    """
    gap, offset = 1, 0
    while True:
        offset = max(0, steps - gap)
        rows = rpc_call(server['url'], server['csrf'], 'GetCascadeTrajectoryGeneratorMetadata',
                        {'cascadeId': sid, 'generatorMetadataOffset': offset},
                        timeout=timeout).get('generatorMetadata') or []
        if rows or offset == 0:
            return (rows[-1] if rows else None), offset + len(rows)
        gap *= 2


def generator_model(entry):
    if not isinstance(entry, dict):
        return None
    planner = entry.get('plannerConfig') if isinstance(entry.get('plannerConfig'), dict) else {}
    requested = planner.get('requestedModel') if isinstance(planner.get('requestedModel'), dict) else {}
    for value in (planner.get('planModel'), requested.get('model'),
                  (entry.get('chatModel') or {}).get('model') if isinstance(entry.get('chatModel'), dict) else None):
        if isinstance(value, str) and value:
            return value
    return None


def read_model_at(server, sid, summary, timeout=5):
    steps = int(summary.get('stepCount') or 0) if isinstance(summary, dict) else 0
    entry, count = last_generator(server, sid, steps, timeout) if steps else (None, 0)
    model_enum = generator_model(entry)
    if not model_enum:
        return {'model': 'unknown', 'reason': 'no generator has run in this conversation yet', 'generators': count}
    return {**describe_model(model_catalog(server, timeout), model_enum),
            'source': 'last generator plannerConfig (the panel model label)', 'generators': count}


def read_model(sid, timeout=5):
    sid = str(uuid.UUID(sid))
    server, row = resolve_agy(sid, timeout=timeout)
    summary = server['summaries'].get(sid) or {}
    return {'session': sid, 'status': row['status'], **read_model_at(server, sid, summary, timeout)}


def switch_config(entry, model_enum):
    """The conversation's own last plannerConfig with only the model changed.

    Sending a bare config resets the rest of the planner (measured: knowledge
    on -> off), so start from what the conversation last ran with.
    """
    planner = entry.get('plannerConfig') if isinstance(entry, dict) else None
    if not isinstance(planner, dict) or not planner:
        raise ModelSwitchError('Antigravity conversation has no planner configuration to preserve; '
                               'let it run one turn before switching its model')
    planner = {k: v for k, v in planner.items() if k not in PLANNER_ONLY_FIELDS}
    planner['requestedModel'] = {'model': model_enum}
    return {'plannerConfig': planner}


def set_model(sid, model, timeout=30, poll=1.0):
    """Switch an idle conversation's model; the switch runs one invocation.

    One attempt. A rejected request changes nothing; a request whose outcome is
    unknown is reported uncertain and never resent.
    """
    if not isinstance(model, str) or not model.strip():
        raise ModelSwitchError('--set must be a nonempty model name')
    sid = str(uuid.UUID(sid))
    server, row = resolve_agy(sid, timeout=min(timeout, 10))
    if row['status'] != 'idle':
        raise ModelSwitchError(f"Antigravity conversation is {row['status']}; a model switch is accepted only while it is idle")
    catalog = model_catalog(server, timeout=min(timeout, 10))
    target = resolve_model(server, model, timeout=min(timeout, 10))
    summary = server['summaries'].get(sid) or {}
    entry, count = last_generator(server, sid, int(summary.get('stepCount') or 0), min(timeout, 10))
    previous = generator_model(entry)
    before = describe_model(catalog, previous) if previous else {'model': 'unknown'}
    if previous == target['model_enum']:
        return {'session': sid, **describe_model(catalog, previous), 'previous_model': before['model'],
                'changed': False, 'applies': 'already current; nothing was sent'}
    frame = {'cascadeId': sid, 'cascadeConfig': switch_config(entry, target['model_enum']),
             'deliveryStrategy': NEXT_INVOCATION}
    try:
        rpc_call(server['url'], server['csrf'], 'SendUserCascadeMessage', frame, timeout=min(timeout, 10))
    except DeliveryError as error:
        # An HTTP status means the server answered and refused; anything else may have landed.
        raise ModelSwitchError(f'Antigravity did not accept the model switch: {error}',
                               uncertain=' HTTP ' not in str(error)) from None
    deadline = time.monotonic() + timeout
    current = None
    def switched(**state):
        return {'session': sid, **describe_model(catalog, current), 'previous_model': before['model'],
                'changed': True, 'applies': 'now; the switch ran one invocation without a user step', **state}

    while True:
        try:
            summary = (rpc_call(server['url'], server['csrf'], 'GetAllCascadeTrajectories', {}, timeout=10)
                       .get('trajectorySummaries') or {}).get(sid, {})
            latest, latest_count = last_generator(server, sid, int(summary.get('stepCount') or 0), 10)
        except DeliveryError as error:
            if current is None:
                raise
            # The switch is already confirmed; only the busy state is unknown.
            return switched(generators_spent=spent, status_after='unknown', busy_after=None,
                            warning=f'The switch landed, but its busy state could not be read: {error}')
        if current is None and latest_count > count and generator_model(latest):
            current = generator_model(latest)
            spent = latest_count - count
            if current != target['model_enum']:
                raise ModelSwitchError(f"Antigravity reads back model {describe_model(catalog, current)['model']!r}, "
                                       f"not {target['model_id']!r}", uncertain=True)
        status = normalize_status(summary.get('status'))
        if current is not None and (status == 'idle' or time.monotonic() >= deadline):
            # The switch is a real turn on the new model (#21): measured 2-8
            # generators and 10-90 s busy. Report what it spent and whether the
            # conversation is still working, so a caller does not retry blindly.
            return switched(generators_spent=latest_count - count, status_after=status, busy_after=status != 'idle')
        if current is None and time.monotonic() >= deadline:
            raise ModelSwitchError('The switch was sent but no new invocation was observed before the timeout; '
                                   'read the model again before resolving', uncertain=True)
        time.sleep(poll)


def build_frame(route, sender, project, text):
    sid = str(uuid.UUID(route['session']))
    label = f"{project or route.get('project') or 'unknown'}/{sender or 'peer'}"
    origin = 'agent-bridge:' + (ORIGIN_CHARS.sub('-', label)[:180] or 'unknown')
    clean_sender = re.sub(r'[\r\n]', ' ', str(sender or 'peer')).strip() or 'peer'
    return {
        'recipient': sid,
        'senderId': origin,
        'displayTitle': f'agent-bridge ({clean_sender})',
        'content': text,
    }


def build_wake_frame(sid):
    return {
        'cascadeId': str(uuid.UUID(sid)),
        'deliveryStrategy': NEXT_INVOCATION,
    }


def wake_session(url, csrf, sid, initial_status=None, timeout=5):
    """Nudge an Antigravity cascade so an inbox message is consumed without delay.

    SendAgentMessage enqueues a high-priority SYSTEM_MESSAGE with NEXT_INVOCATION
    delivery, which a running turn consumes at its next step boundary without
    interrupting active tool execution. However, when a turn has yielded to wait
    on a background command or subagent (status IDLE while SubagentsAndTasksIdle()
    is false) or just crossed the post-invocation boundary into IDLE, the agent
    inbox wakeup is suppressed until all background tasks finish. Calling
    SendUserCascadeMessage with empty items and NEXT_INVOCATION triggers
    setPendingInvocation(steps=[]) when the cascade is not RUNNING, waking the
    turn immediately without injecting a fake USER_INPUT step, while returning a
    benign 'cannot queue empty user input step' when the cascade is already RUNNING.
    """
    frame = build_wake_frame(sid)
    for attempt in range(2 if initial_status == 'running' else 1):
        if attempt:
            time.sleep(WAKE_RETRY_DELAY)
        try:
            rpc_call(url, csrf, 'SendUserCascadeMessage', frame, timeout=timeout)
            return 'woke'
        except DeliveryError as error:
            text = str(error).lower()
            if any(marker in text for marker in BENIGN_WAKE_ERRORS):
                if attempt == 0 and initial_status == 'running':
                    continue
                return 'running'
            return 'ignored'
    return 'running'


def fallback_delivery_title(text, route, target):
    if isinstance(text, str) and text.strip():
        body = text
        if body.startswith('[agent-bridge peer message |') and '\n\n' in body:
            body = body.split('\n\n', 1)[1]
        for raw_line in body.splitlines():
            line = re.sub(r'^#+\s*', '', raw_line.strip()).strip()
            if line and not line.startswith('Answer through the bridge,'):
                return line[:120]
    for candidate in (route.get('name') if isinstance(route, dict) else None,
                      Path(target['cwd']).name if isinstance(target, dict) and target.get('cwd') else None):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:120]
    return None


def deliver(route, message_id, text, sender=None, project=None, timeout=10, dry_run=False):
    mutation_sent = False
    try:
        sid = str(uuid.UUID(route['session']))
        server, target = resolve_agy(sid, timeout=timeout)
        if route.get('cwd') and Path(target['cwd']).resolve() != Path(route['cwd']).expanduser().resolve():
            raise DeliveryError('Resolved Antigravity session cwd differs from the registered route')
        frame = build_frame(route, sender, project, text)
        wake_frame = build_wake_frame(sid)
        if dry_run:
            return {
                'sent': False,
                'transport': 'agy',
                'session': sid,
                'pid': server['pid'],
                'method': 'SendAgentMessage',
                'frame': frame,
                'wake_frame': wake_frame,
            }
        mutation_sent = True
        rpc_call(server['url'], server['csrf'], 'SendAgentMessage', frame, timeout=timeout)
        wake_state = wake_session(server['url'], server['csrf'], sid, target['status'], timeout=timeout)
        if not target.get('name'):
            auto_title = fallback_delivery_title(text, route, target)
            if auto_title:
                try:
                    rpc_call(
                        server['url'],
                        server['csrf'],
                        'UpdateConversationAnnotations',
                        {
                            'cascadeId': sid,
                            'annotations': {'title': auto_title},
                            'mergeAnnotations': True,
                        },
                        timeout=timeout,
                    )
                except DeliveryError:
                    pass
        return {
            'session': sid,
            'pid': server['pid'],
            'status': target['status'],
            'method': 'SendAgentMessage',
            'wake': wake_state,
        }
    except DeliveryError as error:
        if mutation_sent:
            error.uncertain = True
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise DeliveryError(f'Antigravity delivery failed: {error}', uncertain=mutation_sent) from None



# --- Archiving (close) --------------------------------------------------------
# The agent panel's own Archive action sends exactly this annotation; the server
# stamps archivalStatusTimestamp itself. Measured: the conversation leaves the
# panel. Sending archived=false afterwards did not bring it back to the panel,
# so restore is not confirmed and the bridge never offers it.
def archive_agy(sid, timeout=5):
    server, row = resolve_agy(sid, timeout=timeout)
    if row['status'] == 'running':
        raise DeliveryError(f'Antigravity session {sid} is running; archive it after the turn ends')
    try:
        rpc_call(server['url'], server['csrf'], 'UpdateConversationAnnotations',
                 {'cascadeId': row['id'], 'annotations': {'archived': True}, 'mergeAnnotations': True},
                 timeout=timeout)
    except DeliveryError as error:
        # The update may have been applied before the failure; never resend it.
        raise ArchiveUncertain(f'Archive request failed after it was sent; the outcome is unknown: {error}') from None
    try:
        data = rpc_call(server['url'], server['csrf'], 'GetAllCascadeTrajectories', {}, timeout=timeout)
        annotations = ((trajectory_summaries(data) or {}).get(row['id']) or {}).get('annotations') or {}
    except DeliveryError as error:
        raise ArchiveUncertain(f'Archive was sent but could not be read back: {error}') from None
    if annotations.get('archived') is not True:
        raise ArchiveUncertain('Archive was sent but the conversation does not read back as archived')
    return {'closed': True, 'method': 'archive', 'session': row['id'], 'host': server.get('app_dir'),
            'archived_at': annotations.get('archivalStatusTimestamp'), 'restore_confirmed': False,
            'note': 'Hidden from the agent panel. Restoring from outside the panel is not confirmed; '
                    'permanent deletion is left to the owner.'}


class ArchiveUncertain(DeliveryError):
    pass
