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


def validate_override_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or not parsed.port:
        raise DeliveryError('AGENT_BRIDGE_AGY_URL must be a loopback http/https URL with an explicit port')
    return f'{parsed.scheme}://{parsed.hostname}:{parsed.port}'


def _connect_candidate(pid, app_dir, csrf, timeout=5):
    try:
        lsof_out = subprocess.check_output(
            ['lsof', '-Pan', '-p', str(pid), '-iTCP', '-sTCP:LISTEN'],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DeliveryError(f'Failed to inspect listening ports for Antigravity pid {pid}: {error}') from None

    ports = []
    for line in lsof_out.splitlines():
        match = PORT_RE.search(line)
        if match:
            port = int(match.group(1))
            if port not in ports:
                ports.append(port)

    if not ports:
        raise DeliveryError(f'Antigravity {app_dir} language_server (pid {pid}) has no loopback listening port')

    last_error = None
    for port in ports:
        for scheme in ('https', 'http'):
            url = f'{scheme}://127.0.0.1:{port}'
            try:
                data = rpc_call(url, csrf, 'GetAllCascadeTrajectories', {}, timeout=timeout)
                summaries = data.get('trajectorySummaries')
                if isinstance(summaries, dict):
                    return {
                        'pid': pid,
                        'app_dir': app_dir,
                        'url': url,
                        'csrf': csrf,
                        'summaries': summaries,
                    }
            except DeliveryError as error:
                last_error = error
    raise DeliveryError(f'Unable to connect to Antigravity {app_dir} language_server (pid {pid}): {last_error}')


def discover_servers(timeout=5):
    override_url = os.environ.get('AGENT_BRIDGE_AGY_URL')
    override_csrf = os.environ.get('AGENT_BRIDGE_AGY_CSRF')
    if override_url:
        if not override_csrf:
            raise DeliveryError('AGENT_BRIDGE_AGY_CSRF is required when AGENT_BRIDGE_AGY_URL is set')
        url = validate_override_url(override_url)
        data = rpc_call(url, override_csrf, 'GetAllCascadeTrajectories', {}, timeout=timeout)
        summaries = data.get('trajectorySummaries')
        if not isinstance(summaries, dict):
            raise DeliveryError('Antigravity server did not return trajectorySummaries')
        return [{'pid': os.getpid(), 'app_dir': 'antigravity', 'url': url, 'csrf': override_csrf, 'summaries': summaries}]

    uid = os.getuid()
    try:
        ps_out = subprocess.check_output(['ps', '-axo', 'pid=,uid=,command='], text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise DeliveryError(f'Failed to inspect local processes for Antigravity: {error}') from None

    by_app_dir = {}
    for line in ps_out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, proc_uid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        cmd = parts[2]
        if proc_uid != uid or 'language_server' not in cmd or LSP_FLAG_RE.search(cmd):
            continue
        app_match = APP_DIR_RE.search(cmd)
        csrf_match = CSRF_RE.search(cmd)
        if app_match and csrf_match:
            app_dir = app_match.group(1)
            by_app_dir.setdefault(app_dir, []).append((pid, csrf_match.group(1)))

    if not by_app_dir:
        raise DeliveryError('Antigravity language_server (desktop or IDE) is not running')

    for app_dir, items in by_app_dir.items():
        if len(items) > 1:
            raise DeliveryError(f'{len(items)} Antigravity {app_dir} language_server processes found; not choosing one')

    servers = []
    last_error = None
    for app_dir in ('antigravity', 'antigravity-ide'):
        if app_dir not in by_app_dir:
            continue
        pid, csrf = by_app_dir[app_dir][0]
        try:
            servers.append(_connect_candidate(pid, app_dir, csrf, timeout=timeout))
        except DeliveryError as error:
            last_error = error

    if not servers:
        raise last_error or DeliveryError('Unable to connect to any Antigravity language_server')
    return servers


def discover_server(timeout=5):
    return discover_servers(timeout=timeout)[0]


def agy_row(sid, item, pid, fallback_cwd=None, app_dir='antigravity'):
    sid = str(uuid.UUID(sid))
    raw_status = item.get('status', 'unknown') if isinstance(item, dict) else 'unknown'
    cwd = extract_cwd(item.get('workspaces') if isinstance(item, dict) else None) or fallback_cwd
    surface = 'IDE' if app_dir == 'antigravity-ide' else 'desktop'
    return {
        'provider': 'agy',
        'id': sid,
        'name': (item.get('summary') if isinstance(item, dict) else None) or None,
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
    combined = []
    for server in servers:
        for cid, item in server['summaries'].items():
            combined.append((server, cid, item))
    combined.sort(
        key=lambda entry: (entry[2].get('lastModifiedTime') or '') if isinstance(entry[2], dict) else '',
        reverse=True,
    )
    rows = []
    seen_ids = set()
    for server, cid, item in combined:
        try:
            sid = str(uuid.UUID(cid))
        except ValueError:
            continue
        if sid in seen_ids:
            continue
        row = agy_row(sid, item, server['pid'], app_dir=server.get('app_dir', 'antigravity'))
        if not row['cwd']:
            continue
        if target_cwd and str(Path(row['cwd']).resolve()) != target_cwd:
            continue
        if search and search.casefold() not in (row['name'] or '').casefold():
            continue
        seen_ids.add(sid)
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
    prioritized = [s for s in servers if sid in s['summaries']] + [s for s in servers if sid not in s['summaries']]
    matches = []
    last_error = None
    for server in prioritized:
        summary_item = server['summaries'].get(sid)
        meta = None
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
        if not isinstance(meta, dict) and not isinstance(summary_item, dict):
            continue
        meta_cwd = extract_cwd(meta.get('workspaces') if isinstance(meta, dict) else None)
        row = agy_row(
            sid,
            summary_item or {},
            server['pid'],
            fallback_cwd=meta_cwd,
            app_dir=server.get('app_dir', 'antigravity'),
        )
        if not row['cwd']:
            last_error = DeliveryError(f'Antigravity session {sid} has no workspace directory')
            continue
        matches.append((server, row))
        if sid in server['summaries']:
            break

    if not matches:
        raise last_error or DeliveryError(f'Antigravity session {sid} is not found in any live desktop or IDE server')
    if len(matches) > 1:
        raise DeliveryError(f'Antigravity session {sid} matched multiple live servers')
    return matches[0]


def read_agy(sid, timeout=5):
    _, row = resolve_agy(sid, timeout=timeout)
    return {'session': row, 'warning': None}


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

