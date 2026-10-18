"""Read session metadata through installed CLIs; never resume or start a turn."""
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
import uuid

import agy_live


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
    def __init__(self, executable='codex', mode='auto', timeout=10):
        self.executable, self.timeout = resolve_codex_executable(executable), timeout
        self.warning = None
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
            self.call('initialize',{'clientInfo':{'name':'agent_bridge','version':'0.2'}})
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


def open_session(provider='agy', cwd=None, title=None, model=None):
    if provider != 'agy':
        raise DiscoveryError(f'Opening a new session is currently supported only for Antigravity Desktop (agy), not {provider!r}')
    if not cwd:
        raise DiscoveryError('Provide --cwd when opening a new Antigravity Desktop session')
    return agy_live.open_agy(cwd=cwd, title=title, model=model)



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
