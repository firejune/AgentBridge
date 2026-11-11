"""Read session metadata or open supported desktop new-session entrypoints."""
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from urllib.parse import urlencode
import uuid

import agy_live
import codex_live
import codex_new


class DiscoveryError(ValueError):
    pass


class CodexRpcError(DiscoveryError):
    def __init__(self, method, error):
        super().__init__(f'Codex {method}: {error}')
        self.code = error.get('code') if isinstance(error, dict) else None
        self.message = error.get('message') if isinstance(error, dict) else None


def resolve_codex_executable(executable):
    if executable != 'codex' or shutil.which('codex'):
        return executable
    for candidate in (
        '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
        '/Applications/Codex.app/Contents/Resources/codex',
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return executable


def cli_prefix(executable):
    # Support Claude installations defined as a login-shell function while
    # keeping all arguments as separate argv entries.
    if executable == 'claude':
        return ['/bin/bash','-lc','claude "$@"','agent-bridge']
    return [executable]


def cli_json(text):
    # Login shells can emit charset/SGR terminal setup before JSON. Strip only
    # recognized terminal controls; unexpected banners still fail parsing.
    cleaned = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|[()][0-2A-Z])', '', text)
    return json.loads(cleaned)


def observed_now():
    return datetime.now(timezone.utc).isoformat()


def stop_process(process):
    # Terminate only the proxy/temporary server started by this client, not a daemon.
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            process.kill()
        process.wait(timeout=2)


class CodexClient:
    def __init__(self, executable='codex', mode='auto', timeout=10, experimental=False):
        self.executable, self.timeout = resolve_codex_executable(executable), timeout
        self.warning = None
        self.experimental = experimental
        try:
            self.open('shared' if mode == 'auto' else mode)
        except (OSError, DiscoveryError):
            if mode != 'auto':
                raise
            self.warning = 'Shared app-server unavailable; reading saved metadata. Runtime status is unknown.'
            self.open('saved')

    def open(self, mode):
        self.mode, self.buffer, self.sequence = mode, b'', 0
        self.errors = tempfile.TemporaryFile()
        self.selector = selectors.DefaultSelector()
        self.process = None
        try:
            args = ['app-server', 'proxy'] if mode == 'shared' else ['app-server', '--stdio']
            self.process = subprocess.Popen([self.executable,*args],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=self.errors,start_new_session=True)
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            params = {'clientInfo':{'name':'agent_bridge','version':'0.2'}}
            if self.experimental:
                params['capabilities'] = {'experimentalApi':True}
            self.call('initialize',params)
            self.write({'method':'initialized'})
        except BaseException:
            self.close()
            raise

    def write(self, payload):
        try:
            self.process.stdin.write((json.dumps(payload)+'\n').encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise DiscoveryError(f'Codex connection closed: {error}') from error

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.write({'id':request_id,'method':method,'params':params})
        deadline = time.monotonic() + self.timeout
        while True:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n',1)
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeError) as error:
                    raise DiscoveryError('Invalid JSON from Codex app-server') from error
                if not isinstance(obj,dict):
                    raise DiscoveryError('Unexpected Codex app-server response')
                if obj.get('id') != request_id:
                    continue  # Notifications are not replies to our request.
                if 'error' in obj:
                    raise CodexRpcError(method, obj['error'])
                if not isinstance(obj.get('result'),dict):
                    raise DiscoveryError(f'Codex {method}: missing result')
                return obj['result']
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise DiscoveryError(f'Codex {method} timed out')
            chunk = os.read(self.process.stdout.fileno(),65536)
            if not chunk:
                self.errors.seek(0)
                detail = self.errors.read().decode(errors='replace')[-2000:]
                raise DiscoveryError('Codex app-server exited: '+detail)
            self.buffer += chunk
            if len(self.buffer) > 8 * 1024 * 1024:
                raise DiscoveryError('Codex metadata response exceeds 8 MiB')

    def close(self):
        if self.process is not None:
            stop_process(self.process)
            for pipe in (self.process.stdin,self.process.stdout):
                pipe.close()
        self.selector.close()
        self.errors.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def codex_row(item, mode):
    sid = str(uuid.UUID(item['id']))  # sessionId is the session tree, not the queue target.
    status = item.get('status', {})
    raw_status = status.get('type','unknown') if isinstance(status,dict) else 'unknown'
    return {'provider':'codex','id':sid,'name':item.get('name'), 'cwd':item['cwd'],
            'status':raw_status if mode == 'shared' else 'unknown',
            'reported_status':raw_status,'runtime_observed':mode == 'shared',
            'source':item.get('source'),'updated_at':item.get('updatedAt'),
            'observed_at':observed_now(), 'scope':'shared-server' if mode == 'shared' else 'saved-metadata'}


def list_codex(executable='codex', cwd=None, search=None, limit=100, mode='auto'):
    with CodexClient(executable,mode) as client:
        rows, cursor, seen = [], None, set()
        while len(rows) < limit:
            params = {'limit':min(100,limit-len(rows)),'sortKey':'updated_at','useStateDbOnly':True}
            if cwd:
                params['cwd'] = str(Path(cwd).expanduser().resolve())
            if search:
                params['searchTerm'] = search
            if cursor:
                params['cursor'] = cursor
            page = client.call('thread/list',params)
            if not isinstance(page.get('data'),list):
                raise DiscoveryError('Codex thread/list did not return a data array')
            rows.extend(codex_row(item,client.mode) for item in page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if cursor in seen:
                raise DiscoveryError('Codex returned a repeated pagination cursor')
            seen.add(cursor)
        return {'sessions':rows[:limit],'more_available':bool(cursor) or len(rows)>limit,
                'warning':client.warning,'scope':'non-archived interactive threads in the CLI store'}


def read_codex(sid, executable='codex', mode='auto'):
    sid = str(uuid.UUID(sid))
    with CodexClient(executable,mode) as client:
        result = client.call('thread/read',{'threadId':sid,'includeTurns':False})
        item = result.get('thread')
        if not isinstance(item,dict) or item.get('id') != sid:
            raise DiscoveryError('Codex returned a different or missing thread')
        return {'session':codex_row(item,client.mode),'warning':client.warning}


def list_claude(executable='claude', cwd=None, search=None, limit=100):
    cmd = [*cli_prefix(executable),'agents','--json','--all']
    # --cwd is documented for background sessions; apply exact filtering ourselves
    # so interactive and background sessions use the same rule.
    process = subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL,text=True,start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=10)
    except BaseException:
        stop_process(process)
        process.communicate()
        raise
    if process.returncode:
        raise DiscoveryError('Claude session discovery failed: '+stderr[-2000:])
    try:
        data = cli_json(stdout)
    except ValueError as error:
        raise DiscoveryError('Claude agents did not return JSON') from error
    if not isinstance(data,list):
        raise DiscoveryError('Claude agents did not return a session array')
    rows = []
    cwd = str(Path(cwd).expanduser().resolve()) if cwd else None
    for item in data:
        if not isinstance(item,dict) or not item.get('sessionId') or not item.get('cwd'):
            raise DiscoveryError('Claude returned a session without an ID or working directory')
        if cwd and str(Path(item['cwd']).resolve()) != cwd:
            continue
        if search and search.casefold() not in (item.get('name') or '').casefold():
            continue
        rows.append({'provider':'claude','id':str(uuid.UUID(item['sessionId'])),
                     'name':item.get('name'),'cwd':item['cwd'],'status':item.get('status','unknown'),
                     'runtime_observed':True,'kind':item.get('kind'),'pid':item.get('pid'),
                     'observed_at':observed_now(),'scope':'CLI agent registry'})
    return {'sessions':rows[:limit],'more_available':len(rows)>limit,
            'scope':'active interactive/background sessions plus retained completed background sessions',
            'warning':None}


def discover(provider='all', cwd=None, search=None, limit=100,
             codex_executable='codex', claude_executable='claude', codex_mode='auto'):
    if limit <= 0:
        raise ValueError('Limit must be positive')
    results, errors = {}, {}
    for name in (('codex','claude','agy') if provider == 'all' else (provider,)):
        try:
            if name == 'codex':
                results[name] = list_codex(codex_executable,cwd,search,limit,codex_mode)
            elif name == 'claude':
                results[name] = list_claude(claude_executable,cwd,search,limit)
            elif name == 'agy':
                results[name] = agy_live.list_agy(cwd,search,limit)
            else:
                raise ValueError(f'Unknown provider: {name}')
        except (ValueError, OSError, subprocess.TimeoutExpired, KeyError, TypeError) as error:
            errors[name] = str(error)
    return {'providers':results,'errors':errors}


def inspect_session(provider, sid, executable=None, codex_mode='auto'):
    sid = str(uuid.UUID(sid))
    if provider == 'codex':
        return read_codex(sid,executable or 'codex',codex_mode)
    if provider == 'agy':
        return agy_live.read_agy(sid)
    listing = list_claude(executable or 'claude',limit=1000000)
    matches = [row for row in listing['sessions'] if row['id'] == sid]
    if len(matches) != 1:
        raise DiscoveryError('Claude session is absent or ambiguous in the agent registry; not selecting another session')
    return {'session':matches[0],'warning':None}


def open_session(provider='agy', cwd=None, title=None, model=None, timeout=180, host='desktop', ide=None,
                 force_new=False):
    if host != 'desktop' and provider not in ('claude', 'agy'):
        raise DiscoveryError(f'--host {host} applies only to Claude Code (claude / claude-peer) and Antigravity (agy)')
    if ide and (host != 'ide' or provider != 'claude'):
        raise DiscoveryError('--ide picks the IDE of a Claude Code session; it needs --provider claude --host ide')
    if provider == 'codex':
        return codex_new.open_codex(cwd=cwd, title=title, model=model, timeout=timeout, force_new=force_new)
    if provider == 'claude':
        if not cwd:
            raise DiscoveryError('Provide --cwd when opening a new Claude Code session')
        if host == 'ide':
            return open_claude_ide(cwd=cwd, title=title, model=model, timeout=timeout, ide=ide or DEFAULT_IDE)
        if host != 'desktop':
            raise DiscoveryError(f'Unknown Claude host {host!r}; use desktop or ide')
        return open_claude(cwd=cwd, title=title, model=model, timeout=timeout)
    if provider != 'agy':
        raise DiscoveryError(f'Opening a new session is supported only for Antigravity (agy), Claude Code (claude) and Codex desktop (codex), not {provider!r}')
    if not cwd:
        raise DiscoveryError('Provide --cwd when opening a new Antigravity session')
    return agy_live.open_agy(cwd=cwd, title=title, model=model, host=host)



# --- Claude Code live-session registry (peerProtocol 1) ---------------------
# Undocumented internal Claude Code protocol, reverse-read from Claude Code
# 2.1.295. Each live session writes ~/.claude/sessions/<pid>.json with its
# sessionId and messagingSocketPath. Anything unexpected is a loud failure:
# the bridge never guesses another session or another protocol version.
PEER_PROTOCOL = 1


def claude_sessions_dir():
    base = os.environ.get('CLAUDE_CONFIG_DIR')
    return Path(base).expanduser() / 'sessions' if base else Path.home() / '.claude' / 'sessions'


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def resolve_claude_peer(sid, sessions_dir=None):
    """Return the live peer endpoint of one exact Claude sessionId, or raise."""
    sid = str(uuid.UUID(sid))
    root = Path(sessions_dir) if sessions_dir else claude_sessions_dir()
    if not root.is_dir():
        raise DiscoveryError(f'Claude session registry is missing: {root}')
    records = []
    for path in root.iterdir():
        if not re.fullmatch(r'\d+\.json', path.name):
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue  # Files are rewritten while sessions update their status.
        if isinstance(record, dict) and record.get('sessionId') == sid:
            records.append((path, record))
    if not records:
        raise DiscoveryError(f'No Claude session file has sessionId {sid}; the session is not running')
    live = [(p, r) for p, r in records if isinstance(r.get('pid'), int)
            and p.name == f"{r['pid']}.json" and pid_alive(r['pid'])]
    if not live:
        raise DiscoveryError(f'Claude session {sid} is not alive (pid {records[0][1].get("pid")!r} is gone)')
    if len(live) > 1:
        raise DiscoveryError(f'{len(live)} live Claude processes claim session {sid}; not choosing one')
    path, record = live[0]
    if record.get('peerProtocol') != PEER_PROTOCOL:
        raise DiscoveryError(f'Claude session {sid} speaks peerProtocol {record.get("peerProtocol")!r}; '
                             f'the bridge supports only {PEER_PROTOCOL}')
    sock = record.get('messagingSocketPath')
    if not isinstance(sock, str) or not sock.startswith('/'):
        raise DiscoveryError(f'Claude session {sid} has no messaging socket (inbox not running)')
    return {'provider':'claude','id':sid,'name':record.get('name'),'cwd':record.get('cwd'),
            'status':record.get('status','unknown'),'pid':record['pid'],'socket':sock,
            'peer_protocol':record['peerProtocol'],'version':record.get('version'),
            'registry':str(path),'observed_at':observed_now(),'scope':'Claude live-session registry'}


TITLE_MARK = b'"custom-title"'
# The IDE session list reads only the first and last 64 KiB of a transcript, and a running CLI adopts a
# title another process wrote only when it reads its own tail (after ~32 KB of its writes). A record
# outside the tail is invisible to both, so it is appended again until the session re-emits it (#89).
TITLE_TAIL = 65536


def claude_title(sid, title, force=False, projects_dir=None):
    """Write `title` as the Claude session's custom title (the record /rename writes), best-effort.

    The IDE/CLI resume picker labels a session by its custom title, else by its first prompt; bridge
    sessions all start with the same injected peer line, so an untitled dead tower cannot be told apart.
    Never overwrites an existing custom title unless `force`. Works on dead sessions (transcript only).
    Returns {'state': 'written'|'kept'|'skipped', ...}; never raises for an expected miss.
    """
    sid = str(uuid.UUID(sid))
    if not isinstance(title, str) or not title.strip():
        return {'state':'skipped','reason':'empty title'}
    title = title.strip()
    base = os.environ.get('CLAUDE_CONFIG_DIR')
    root = Path(projects_dir) if projects_dir else (Path(base).expanduser() if base else Path.home() / '.claude') / 'projects'
    paths = sorted(root.glob(f'*/{sid}.jsonl'))
    if len(paths) != 1:
        return {'state':'skipped','reason':('no transcript yet' if not paths else f'{len(paths)} transcripts') + f' for {sid}'}
    path = paths[0]
    try:
        data = path.read_bytes()
    except OSError as error:
        return {'state':'skipped','reason':f'transcript unreadable: {error}'}
    current, at = None, -1
    start = data.find(TITLE_MARK)
    while start != -1:
        lo = data.rfind(b'\n', 0, start) + 1
        hi = data.find(b'\n', start)
        hi = len(data) if hi == -1 else hi
        try:
            entry = json.loads(data[lo:hi])
        except ValueError:
            entry = None
        if isinstance(entry, dict) and entry.get('type') == 'custom-title' and isinstance(entry.get('customTitle'), str):
            current, at = entry['customTitle'], lo
        start = data.find(TITLE_MARK, hi)
    in_tail = at >= len(data) - TITLE_TAIL
    if current == title and in_tail and not force:
        return {'state':'kept','title':current,'transcript':str(path)}
    if current is not None and current != title and not force:
        return {'state':'kept','title':current,'transcript':str(path),'reason':'session already has a title'}
    if data and not data.endswith(b'\n'):
        return {'state':'skipped','reason':'transcript tail is mid-write; retry later','transcript':str(path)}
    # Compact separators, byte-identical to the records Claude Code writes itself.
    line = json.dumps({'type':'custom-title','customTitle':title,'sessionId':sid},
                      ensure_ascii=False, separators=(',',':')) + '\n'
    try:
        with path.open('ab') as handle:  # one O_APPEND write, like the session's own appends
            handle.write(line.encode('utf-8'))
    except OSError as error:
        return {'state':'skipped','reason':f'transcript not writable: {error}','transcript':str(path)}
    found = {'state':'written','title':title,'previous':current,'transcript':str(path)}
    if current == title:
        found['reason'] = 'refreshed into the transcript tail'
    return found


def route_title(route):
    return f"{route.get('project')}/{route.get('name')}"


def resolve_agy_peer(sid, timeout=5):
    """Return the live trajectory row of one exact Antigravity conversationId, or raise."""
    _, row = agy_live.resolve_agy(sid, timeout=timeout)
    return row


def resolve_codex_peer(sid, executable='codex', mode='auto'):
    """Return the live Codex Desktop thread row for one exact thread UUID, or raise."""
    info = read_codex(sid, executable=executable, mode=mode)['session']
    if info.get('status') in ('idle', 'ready', 'running', 'busy'):
        return info
    try:
        with codex_live.Client(timeout=0.5) as client:
            client.owner(info['id'])
        return dict(info, status='idle', runtime_observed=True)
    except (codex_live.DeliveryError, OSError) as error:
        raise DiscoveryError(f'Codex session {sid} has no live Desktop owner: {error}') from None


def list_agy_peers(limit=25):
    """Return live Antigravity trajectory rows (idle or running) across local servers."""
    listing = agy_live.list_agy(limit=limit)
    return [dict(r, provider='agy') for r in listing.get('sessions', [])
            if r.get('status') in ('idle', 'ready', 'running', 'busy')]


def list_claude_peers(sessions_dir=None):
    """Return live Claude peer sessions from the local session directory without spawning a CLI."""
    root = Path(sessions_dir) if sessions_dir else claude_sessions_dir()
    rows = []
    for record in claude_live_records(root):
        sock = record.get('messagingSocketPath')
        if record.get('peerProtocol') != PEER_PROTOCOL or not isinstance(sock, str) or not sock.startswith('/'):
            continue
        try:
            sid = str(uuid.UUID(record['sessionId']))
        except (ValueError, KeyError, TypeError):
            continue
        if record.get('cwd'):
            rows.append({'provider': 'claude', 'id': sid, 'name': record.get('name'),
                         'cwd': record['cwd'], 'status': record.get('status', 'idle'),
                         'pid': record['pid'], 'observed_at': observed_now()})
    return rows


def list_codex_peers(limit=25, executable='codex', mode='auto'):
    """Return Codex threads that are actively owned by Codex Desktop (or active in shared server)."""
    if mode != 'shared' and not os.environ.get('AGENT_BRIDGE_CODEX_URL'):
        try:
            codex_live.validate_endpoint(codex_live.endpoint())
        except (codex_live.DeliveryError, OSError):
            return []
    listing = list_codex(executable=executable, limit=limit, mode=mode)
    live, candidates = [], []
    for row in listing['sessions']:
        if row.get('status') in ('idle', 'ready', 'running', 'busy'):
            live.append(row)
        else:
            candidates.append(row)
    if candidates:
        try:
            with codex_live.Client(timeout=0.15) as client:
                for row in candidates[:3]:
                    try:
                        client.owner(row['id'])
                        live.append(dict(row, status='idle', runtime_observed=True))
                    except (codex_live.DeliveryError, OSError):
                        continue
        except (codex_live.DeliveryError, OSError):
            pass
    return live


# --- Opening a new Claude Code desktop session ------------------------------
# The desktop app registers the claude:// scheme. Its own Finder service
# ("New Claude Code Session Here") opens claude://code/new?folder=PATH, and the
# handler also accepts q= (or prompt=) to prefill the composer. The link only
# opens the new-session composer: the session starts when the owner presses
# Enter. The bridge never presses it and never launches a standalone CLI;
# it opens the composer and waits for the new live registry record.
# Trusting a folder also starts a short-lived probe CLI there (the app logs it
# as a "temporary query"); its record has no hostSessionId. Only a record whose
# hostSessionId names a desktop session (local_...) is the new session.
CLAUDE_DESKTOP_ENTRYPOINT = 'claude-desktop'


def claude_new_session_url(cwd, prompt):
    query = [('folder', cwd)]
    if prompt:
        query.append(('q', prompt))
    query.append(('source', 'url_external'))
    return 'claude://code/new?' + urlencode(query)


def open_url(url):
    if not shutil.which('open'):
        raise DiscoveryError('Opening a new Claude Code session needs the macOS `open` command')
    result = subprocess.run(['open', url], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, timeout=10)
    if result.returncode:
        raise DiscoveryError(f'The link {url.split(":", 1)[0]}:// was not opened: ' + result.stderr.strip()[-500:])


def claude_live_records(root):
    rows = []
    if not root.is_dir():
        return rows
    for path in root.iterdir():
        if not re.fullmatch(r'\d+\.json', path.name):
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if (isinstance(record, dict) and isinstance(record.get('sessionId'), str)
                and isinstance(record.get('pid'), int) and path.name == f"{record['pid']}.json"
                and pid_alive(record['pid'])):
            rows.append(record)
    return rows


def same_or_inside(path, folder):
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return False
    return resolved == folder or folder in resolved.parents


def open_claude(cwd, title=None, model=None, timeout=180, sessions_dir=None, opener=None, poll=0.5):
    if model:
        raise DiscoveryError('The Claude desktop new-session link carries no model; choose it in the composer instead of --model')
    if not cwd or not str(cwd).strip():
        raise DiscoveryError('Workspace --cwd is required to open a Claude Code desktop session')
    if not 0 < timeout <= 3600:
        raise DiscoveryError('Timeout must be between 0 and 3600 seconds')
    folder = Path(cwd).expanduser().resolve()
    if not folder.is_dir():
        raise DiscoveryError(f'Workspace directory does not exist: {folder}')
    root = Path(sessions_dir) if sessions_dir else claude_sessions_dir()
    clean_title = str(title).strip() if isinstance(title, str) and title.strip() else None
    prompt = clean_title or 'agent-bridge: this session was opened to receive peer messages. Wait for them.'
    before = {r['sessionId'] for r in claude_live_records(root)}
    opened_at = time.time() * 1000
    url = claude_new_session_url(str(folder), prompt)
    (opener or open_url)(url)
    deadline = time.monotonic() + timeout
    while True:
        fresh = [r for r in claude_live_records(root)
                 if r['sessionId'] not in before
                 and r.get('entrypoint') == CLAUDE_DESKTOP_ENTRYPOINT
                 and isinstance(r.get('hostSessionId'), str) and r['hostSessionId'].startswith('local_')
                 and not (isinstance(r.get('startedAt'), (int, float)) and r['startedAt'] < opened_at - 5000)
                 and same_or_inside(r.get('cwd'), folder)]
        ids = {r['sessionId'] for r in fresh}
        if len(ids) > 1:
            raise DiscoveryError(f'{len(ids)} new Claude sessions appeared under {folder}; not choosing one '
                                 '(bind the right one with register --discover --session ID)')
        if ids:
            sid = ids.pop()
            try:
                row = resolve_claude_peer(sid, root)
            except DiscoveryError:
                row = None  # The record exists before the inbox socket is announced.
            if row:
                host = next(r['hostSessionId'] for r in fresh if r['sessionId'] == sid)
                return {'created': True, 'session': row, 'desktop_session': host,
                        'url': url, 'prompt': prompt, 'warning': None}
        if time.monotonic() >= deadline:
            raise DiscoveryError(f'No new live Claude Code session appeared under {folder} within {timeout:g}s. '
                                 'The composer opens with the first message prefilled; the session starts only '
                                 'when someone presses Enter in the app. Then bind it with register --discover --session ID.')
        time.sleep(poll)


# --- Opening a new Claude Code IDE session ----------------------------------
# The Claude Code extension (VS Code, Cursor, Antigravity IDE) registers a URI
# handler: <scheme>://anthropic.claude-code/open?prompt=TEXT opens a new
# conversation tab and only prefills the composer. Measured on extension
# 2.1.309: the tab starts its CLI at once, so a live registry record with
# entrypoint claude-vscode and a messaging socket appears with no Enter, and a
# peer push wakes it. The link has no folder parameter; the IDE routes it to
# the last active window. So the bridge first runs the IDE's own CLI with the
# folder (which focuses the window holding it, or opens one), waits, opens the
# link, and accepts only a new record under that folder. A new record under
# another folder is a stray tab in the wrong window: reported, never bound.
CLAUDE_IDE_ENTRYPOINT = 'claude-vscode'
CLAUDE_IDE_EXTENSION = 'anthropic.claude-code'
DEFAULT_IDE = 'antigravity-ide'
CLAUDE_IDES = {
    'antigravity-ide': {'scheme': 'antigravity-ide', 'cli': 'antigravity-ide',
                        'bundle': '/Applications/Antigravity IDE.app/Contents/Resources/app/bin/antigravity-ide'},
    'vscode': {'scheme': 'vscode', 'cli': 'code',
               'bundle': '/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code'},
    'cursor': {'scheme': 'cursor', 'cli': 'cursor',
               'bundle': '/Applications/Cursor.app/Contents/Resources/app/bin/cursor'},
}


def claude_ide_url(ide, prompt):
    query = urlencode([('prompt', prompt)]) if prompt else ''
    return f"{CLAUDE_IDES[ide]['scheme']}://{CLAUDE_IDE_EXTENSION}/open" + (f'?{query}' if query else '')


def ide_cli(ide):
    spec = CLAUDE_IDES[ide]
    found = shutil.which(spec['cli'])
    if found:
        return found
    if os.access(spec['bundle'], os.X_OK):
        return spec['bundle']
    raise DiscoveryError(f"The {ide} command line ({spec['cli']}) was not found; install it from the IDE "
                         'so the bridge can focus the window that holds the folder')


def focus_ide_folder(ide, folder):
    result = subprocess.run([ide_cli(ide), str(folder)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, timeout=30)
    if result.returncode:
        raise DiscoveryError(f'{ide} did not open {folder}: ' + result.stderr.strip()[-500:])


def open_claude_ide(cwd, title=None, model=None, timeout=180, ide=DEFAULT_IDE, sessions_dir=None,
                    opener=None, focuser=None, settle=3.0, poll=0.5):
    if model:
        raise DiscoveryError('The Claude IDE new-session link carries no model; the session starts on the '
                             "IDE's selected model")
    if ide not in CLAUDE_IDES:
        raise DiscoveryError(f'Unknown IDE {ide!r}; use one of: ' + ', '.join(sorted(CLAUDE_IDES)))
    if not cwd or not str(cwd).strip():
        raise DiscoveryError('Workspace --cwd is required to open a Claude Code IDE session')
    if not 0 < timeout <= 3600:
        raise DiscoveryError('Timeout must be between 0 and 3600 seconds')
    folder = Path(cwd).expanduser().resolve()
    if not folder.is_dir():
        raise DiscoveryError(f'Workspace directory does not exist: {folder}')
    root = Path(sessions_dir) if sessions_dir else claude_sessions_dir()
    # No one presses Enter in the IDE: a pre-filled prompt stays unsent in the composer (issue #87).
    # The session is labelled with its route name on register instead.
    ignored = str(title).strip() if isinstance(title, str) and title.strip() else None
    prompt = None
    url = claude_ide_url(ide, prompt)
    (focuser or focus_ide_folder)(ide, folder)
    if settle:
        time.sleep(settle)  # A window opened just now must become the active one before the link lands.
    before = {r['sessionId'] for r in claude_live_records(root)}
    opened_at = time.time() * 1000
    (opener or open_url)(url)
    deadline = time.monotonic() + timeout
    while True:
        fresh = [r for r in claude_live_records(root)
                 if r['sessionId'] not in before
                 and r.get('entrypoint') == CLAUDE_IDE_ENTRYPOINT
                 and not (isinstance(r.get('startedAt'), (int, float)) and r['startedAt'] < opened_at - 5000)]
        mine = {r['sessionId'] for r in fresh if same_or_inside(r.get('cwd'), folder)}
        stray = [{'id': r['sessionId'], 'cwd': r.get('cwd'), 'pid': r['pid']}
                 for r in fresh if r['sessionId'] not in mine]
        if stray and not mine:
            raise DiscoveryError(f'A new Claude IDE session opened under {stray[0]["cwd"]}, not {folder}: the link '
                                 'reached another window. It is not registered and is left open (close its tab). '
                                 'stray=' + json.dumps(stray))
        if len(mine) > 1:
            raise DiscoveryError(f'{len(mine)} new Claude IDE sessions appeared under {folder}; not choosing one '
                                 '(bind the right one with register --discover --session ID)')
        if mine:
            sid = next(iter(mine))
            try:
                row = resolve_claude_peer(sid, root)
            except DiscoveryError:
                row = None  # The record exists before the inbox socket is announced.
            if row:
                warning = (f'--title {ignored!r} is ignored on --host ide (nothing would send it); '
                           'the session is labelled with its route name') if ignored else None
                return {'created': True, 'session': row, 'host': 'ide', 'ide': ide,
                        'url': url, 'prompt': prompt, 'warning': warning}
        if time.monotonic() >= deadline:
            raise DiscoveryError(f'No new live Claude IDE session appeared under {folder} within {timeout:g}s. '
                                 f'Check that {ide} is running with the Claude Code extension enabled.')
        time.sleep(poll)


# --- Closing a Claude Code session ------------------------------------------
# There is no external clean exit: the peer socket accepts only user frames, so
# /exit is unreachable. Measured on the IDE extension 2.1.310 and on desktop:
# SIGTERM ends the CLI, its registry record goes away and nothing respawns; the
# transcript stays, and a new message in that session resumes the same
# sessionId (desktop keeps the same hostSessionId). That makes it a close that
# can be undone.
CLOSABLE_CLAUDE_HOSTS = {
    'claude-vscode': 'The IDE tab shows "process exited with code 143". The transcript is kept; '
                     'typing in that tab resumes the same session.',
    'claude-desktop': 'The desktop app shows "session interrupted" with a retry button. The transcript is '
                      'kept; sending a message in that session resumes the same session.',
}


def claude_desktop_sessions_dir():
    base = os.environ.get('CLAUDE_DESKTOP_SESSIONS_DIR')
    if base:
        return Path(base).expanduser()
    return Path.home() / 'Library' / 'Application Support' / 'Claude' / 'claude-code-sessions'


def archive_claude_desktop(host_session_id, cli_session_id=None, desktop_dir=None):
    root = Path(desktop_dir) if desktop_dir else claude_desktop_sessions_dir()
    if not root.is_dir():
        raise DiscoveryError(f'Claude Desktop session directory is missing: {root}')
    target_name = (
        f'{host_session_id}.json'
        if isinstance(host_session_id, str) and re.fullmatch(r'local_[0-9a-fA-F-]+', host_session_id)
        else None
    )
    matched = []
    for path in root.rglob('local_*.json'):
        if target_name and path.name == target_name:
            matched.append(path)
            continue
        if cli_session_id and not target_name:
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get('cliSessionId') == cli_session_id:
                matched.append(path)
    if not matched:
        raise DiscoveryError(
            f'Claude Desktop metadata for session {host_session_id or cli_session_id} was not found under {root}'
        )
    for path in matched:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise DiscoveryError(f'Invalid Claude Desktop metadata in {path}')
            data['isArchived'] = True
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, path)
            readback = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise DiscoveryError(f'Failed to archive Claude Desktop session {path.name}: {error}') from None
        if not isinstance(readback, dict) or readback.get('isArchived') is not True:
            raise DiscoveryError(f'Claude Desktop session {path.name} did not read back as archived')
    return True


def close_claude(sid, own_session=None, timeout=10, sessions_dir=None, killer=None, poll=0.2,
                 archive=False, desktop_dir=None):
    row = resolve_claude_peer(sid, sessions_dir)
    if own_session and str(own_session) == row['id']:
        raise DiscoveryError('Refusing to close the calling session itself')
    try:
        record = json.loads(Path(row['registry']).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise DiscoveryError(f'Cannot read the registry record of Claude session {sid}: {error}') from None
    host = record.get('entrypoint') or 'unknown'
    if host not in CLOSABLE_CLAUDE_HOSTS:
        raise DiscoveryError(f'unsupported host {host}')
    pid = row['pid']
    (killer or os.kill)(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            raise DiscoveryError(f'Claude session {sid} (pid {pid}) is still running {timeout:g}s after SIGTERM; '
                                 'not escalating. Check it before trying again.')
        time.sleep(poll)
    if archive and host == 'claude-desktop':
        archive_claude_desktop(record.get('hostSessionId'), cli_session_id=row['id'], desktop_dir=desktop_dir)
        return {
            'closed': True,
            'method': 'archive',
            'archived': True,
            'session': row['id'],
            'host': host,
            'pid': pid,
            'resumable': True,
            'note': 'Terminated CLI process and marked isArchived=true in Claude Desktop session metadata.',
        }
    return {
        'closed': True,
        'method': 'terminate',
        'archived': False,
        'session': row['id'],
        'host': host,
        'pid': pid,
        'resumable': True,
        'note': CLOSABLE_CLAUDE_HOSTS[host],
    }


# --- Waking an idle-closed Claude Code session (issue #115) -----------------
# Measured on Claude Desktop with CLI 2.1.317 (main.log): the app itself closes
# sessions. "[WarmLifecycle:session] Idle timeout reached" pauses a hidden
# session 900 s after its last turn ("Pausing session ... (idle_timeout)"), and
# "[CliGovernor] pressure evicting" pauses the longest-idle one under memory
# pressure ("(governor_evict)"). Either way the CLI exits and its registry
# record and inbox socket go away; the transcript and the Desktop metadata
# (claude-code-sessions/**/local_*.json, cliSessionId) stay.
# claude://claude.ai/epitaxy/<local_id> focuses that session in the app, which
# warms it: the app spawns its own CLI with --resume <cliSessionId>, the same
# sessionId and hostSessionId come back, and the CLI binds its inbox during
# startup, before any turn. Exception: the app starts sessions that descend
# from a scheduled task with CLAUDE_CODE_HARBOR_KITE=0, which turns the
# cross-session inbox off, so a woken copy of one never gets a socket.
# The bridge never starts a CLI itself and never types into the composer.
WAKE_STATES = ('woken', 'already-live', 'unsupported', 'failed', 'uncertain')
WAKE_TIMEOUT = 30.0  # seconds a wake waits for the woken session's inbox (measured ~4 s)
DESKTOP_HEAD = 16384  # cliSessionId sits near the top of a Desktop metadata file
TRANSCRIPT_TAIL = 262144


def claude_desktop_wake_url(host_session_id):
    return f'claude://claude.ai/epitaxy/{host_session_id}'


def find_claude_desktop_session(sid, desktop_dir=None):
    """Return the Desktop session whose cliSessionId is `sid`, or None. Never chooses between two."""
    sid = str(uuid.UUID(sid))
    root = Path(desktop_dir) if desktop_dir else claude_desktop_sessions_dir()
    if not root.is_dir():
        return None
    needle = re.compile(rb'"cliSessionId"\s*:\s*"' + re.escape(sid.encode()) + rb'"')
    found = {}
    for path in root.rglob('local_*.json'):
        try:
            real = path.resolve()
            if real in found:
                continue
            with open(real, 'rb') as handle:
                if not needle.search(handle.read(DESKTOP_HEAD)):
                    continue
            data = json.loads(real.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        host = data.get('sessionId')
        if (data.get('cliSessionId') == sid and isinstance(host, str)
                and re.fullmatch(r'local_[0-9a-fA-F-]+', host) and path.name == f'{host}.json'):
            found[real] = data
    hosts = {data['sessionId'] for data in found.values()}
    if not hosts:
        return None
    if len(hosts) > 1:
        raise DiscoveryError(f'{len(hosts)} Claude Desktop sessions claim CLI session {sid}; not choosing one')
    rows = list(found.values())
    return {'host_session_id': hosts.pop(),
            'archived': any(row.get('isArchived') is True for row in rows),
            'scheduled_task': next((row['scheduledTaskId'] for row in rows if row.get('scheduledTaskId')), None),
            'cwd': next((row['cwd'] for row in rows if isinstance(row.get('cwd'), str)), None)}


def claude_transcript_host(sid, projects_dir=None):
    """The entrypoint the session's transcript last recorded (e.g. claude-vscode), or None."""
    sid = str(uuid.UUID(sid))
    base = os.environ.get('CLAUDE_CONFIG_DIR')
    root = Path(projects_dir) if projects_dir else (Path(base).expanduser() if base else Path.home() / '.claude') / 'projects'
    paths = sorted(root.glob(f'*/{sid}.jsonl'))
    if len(paths) != 1:
        return None
    try:
        with paths[0].open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRANSCRIPT_TAIL))
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get('sessionId') == sid and isinstance(entry.get('entrypoint'), str):
            return entry['entrypoint']
    return None


WAKE_OPEN_TIMEOUT = 10.0  # upper bound for `open -g` itself
WAKE_OPEN_FLOOR = 0.25  # least time `open -g` is given when the wake budget is nearly spent


def open_url_background(url, timeout=WAKE_OPEN_TIMEOUT):
    """Hand a link to its app without asking macOS to raise the app (`open -g`)."""
    if not shutil.which('open'):
        raise DiscoveryError('Waking a Claude Code desktop session needs the macOS `open` command')
    result = subprocess.run(['open', '-g', url], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, timeout=timeout)
    if result.returncode:
        raise DiscoveryError(f'The link {url.split(":", 1)[0]}:// was not opened: ' + result.stderr.strip()[-500:])


def wake_claude(sid, timeout=WAKE_TIMEOUT, sessions_dir=None, desktop_dir=None, projects_dir=None,
                opener=None, poll=0.25):
    """Bring one idle-closed Claude Code session back in its own host, then wait for its inbox.

    Returns {'state': woken | already-live | unsupported | failed | uncertain, 'reason', ...}. Only `woken`
    and `already-live` mean a push can go out now. `failed` means the opener reported that the link was
    not opened (non-zero `open -g` exit or no `open`). `uncertain` means the link may have been handed to
    the app but the session's inbox did not appear in time: the bridge does not open it again or push on
    its own.
    """
    sid = str(uuid.UUID(sid))
    if not 0 < timeout <= 600:
        raise DiscoveryError('Wake timeout must be between 0 and 600 seconds')
    deadline = time.monotonic() + timeout  # Lookup, the opener and the inbox wait all share it.
    root = Path(sessions_dir) if sessions_dir else claude_sessions_dir()
    base = {'session': sid}
    try:
        return {**base, 'state': 'already-live', 'host': None, 'reason': 'the session inbox is running',
                'peer': resolve_claude_peer(sid, root)}
    except DiscoveryError as error:
        closed_reason = str(error)
    live = [r for r in claude_live_records(root) if r['sessionId'] == sid]
    if live:
        # Alive but unreachable: a link cannot restart a running process, so waking cannot help.
        host = live[0].get('entrypoint') or 'unknown'
        reason = closed_reason
        if not live[0].get('messagingSocketPath'):
            reason = ('the session is running with its cross-session inbox off (Claude Desktop turns it off '
                      'for scheduled-task sessions); a wake cannot turn it on')
        return {**base, 'state': 'unsupported', 'host': host, 'reason': reason}
    desktop = find_claude_desktop_session(sid, desktop_dir)
    if desktop is None:
        host = claude_transcript_host(sid, projects_dir)
        if host == CLAUDE_IDE_ENTRYPOINT:
            return {**base, 'state': 'unsupported', 'host': host,
                    'reason': 'IDE wake is not measured: the extension opens a saved session from '
                              '<scheme>://anthropic.claude-code/open?session=ID, but whether that starts its CLI '
                              'and inbox without a turn, and in which window it lands, is unverified. '
                              'Open the session in its IDE tab.'}
        return {**base, 'state': 'unsupported', 'host': host,
                'reason': 'no Claude Desktop session maps to this CLI session'
                          + (f' (its transcript names host {host})' if host else ' and no transcript names its host')}
    host_id = desktop['host_session_id']
    found = {**base, 'host': CLAUDE_DESKTOP_ENTRYPOINT, 'desktop_session': host_id}
    if desktop['archived']:
        return {**found, 'state': 'unsupported', 'reason': 'the Claude Desktop session is archived; unarchive it first'}
    if desktop['scheduled_task']:
        return {**found, 'state': 'unsupported',
                'reason': 'the session belongs to a scheduled task; Claude Desktop starts those with the '
                          'cross-session inbox off (CLAUDE_CODE_HARBOR_KITE=0), so a woken copy has no peer socket'}
    url = claude_desktop_wake_url(host_id)
    started = time.monotonic()
    remaining = deadline - started
    if remaining <= 0:
        return {**found, 'state': 'unsupported',
                'reason': f'the {timeout:g}s wake budget was spent finding the session; the link was not opened'}
    try:
        # `open -g` never outlives the wake budget (a small floor lets it run at all).
        (opener or open_url_background)(url, timeout=max(WAKE_OPEN_FLOOR, min(WAKE_OPEN_TIMEOUT, remaining)))
    except DiscoveryError as error:
        # The opener ran and reported failure (issue #121: this escaped the wake before, so the
        # outcome was recorded only as a push note). Nothing was written to any peer.
        return {**found, 'url': url, 'state': 'failed', 'elapsed': round(time.monotonic() - started, 2),
                'reason': f'the link was not opened: {error}; nothing was sent'}
    except (subprocess.SubprocessError, OSError) as error:
        # The link may or may not have reached the app; nothing was written to any peer.
        return {**found, 'url': url, 'state': 'uncertain', 'elapsed': round(time.monotonic() - started, 2),
                'reason': f'opening the link failed ({type(error).__name__}); nothing was sent'}
    found['url'] = url
    while True:
        records = [r for r in claude_live_records(root) if r['sessionId'] == sid]
        if records:
            mismatch = [r for r in records if r.get('entrypoint') != CLAUDE_DESKTOP_ENTRYPOINT
                        or r.get('hostSessionId') != host_id]
            if mismatch:
                return {**found, 'state': 'uncertain',
                        'reason': 'a live record for this session appeared under another host or Desktop session; '
                                  'not pushing into it'}
            try:
                peer = resolve_claude_peer(sid, root)
            except DiscoveryError:
                peer = None  # The record is written before the inbox socket is announced.
            if peer:
                return {**found, 'state': 'woken', 'reason': 'the session restarted in Claude Desktop',
                        'peer': peer, 'elapsed': round(time.monotonic() - started, 2)}
        if time.monotonic() >= deadline:
            what = ('the session restarted but announced no messaging socket' if records
                    else 'no live record for the session appeared')
            return {**found, 'state': 'uncertain', 'elapsed': round(time.monotonic() - started, 2),
                    'reason': f'opened the session link; {what} within {timeout:g}s. Not reopening it; '
                              'check the session in Claude Desktop'}
        time.sleep(poll)
