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
import codex_new


class DiscoveryError(ValueError):
    pass


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
                    raise DiscoveryError(f'Codex {method}: {obj["error"]}')
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


def open_session(provider='agy', cwd=None, title=None, model=None, timeout=180, host='desktop', ide=None):
    if host != 'desktop' and provider not in ('claude', 'agy'):
        raise DiscoveryError(f'--host {host} applies only to Claude Code (claude / claude-peer) and Antigravity (agy)')
    if ide and (host != 'ide' or provider != 'claude'):
        raise DiscoveryError('--ide picks the IDE of a Claude Code session; it needs --provider claude --host ide')
    if provider == 'codex':
        return codex_new.open_codex(cwd=cwd, title=title, model=model, timeout=timeout)
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
    prompt = str(title).strip() if isinstance(title, str) and title.strip() else None
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
                return {'created': True, 'session': row, 'host': 'ide', 'ide': ide,
                        'url': url, 'prompt': prompt, 'warning': None}
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


def close_claude(sid, own_session=None, timeout=10, sessions_dir=None, killer=None, poll=0.2):
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
    return {'closed': True, 'method': 'terminate', 'session': row['id'], 'host': host, 'pid': pid,
            'resumable': True, 'note': CLOSABLE_CLAUDE_HOSTS[host]}
