"""Opt-in single-principal stdio facade. Broker persistence policy is unchanged.

SQLite owns the durable queue; a single process holds its lock. Once a dispatch
intent is committed it is never replayed. Restart only observes known messages.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
import uuid

import broker
import bridge
import sessions

TOOLS = json.loads(Path(__file__).with_name('mcp_phase1_tools.json').read_text())['tools']
SCOPES = {'bridge_status':'bridge:read', 'bridge_sessions_list':'bridge:read',
          'bridge_operation_get':'bridge:read', 'bridge_session_create':'bridge:session:create',
          'bridge_message_send':'bridge:message:send'}
ACTIVE = ('accepted', 'running', 'unknown')
STATES = {'pending','dispatching','submitted','received','in_progress','answered','done','failed','uncertain'}
RETENTION = 720 * 3600  # Detailed records >=168h, full keys >=720h; tombstones never deleted.
MAX_RESULT = 1024 * 1024


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def opaque(prefix):
    return prefix + secrets.token_urlsafe(24)


class Fault(Exception):
    def __init__(self, code, message=None):
        self.payload = {'code':code, 'message':message or code.replace('_', ' ').capitalize(), 'retryable':False}
        super().__init__(self.payload['message'])


def private_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
        os.close(fd)
        raise ValueError('Private regular file owned by current user required')
    return fd


def load_config(path):
    s = os.lstat(path)
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
        raise ValueError('Phase 1 configuration must be owner-only (0600)')
    config = json.loads(Path(path).read_text())
    if set(config) - {'pinned_sessions'} != {'principal','scopes','workspaces','state_dir','tunnel_id','writes_enabled'}:
        raise ValueError('Unexpected Phase 1 configuration fields')
    if config['writes_enabled'] is not True or not config['principal'] or not config['tunnel_id']:
        raise ValueError('An approved private tunnel owner mapping is required')
    if set(config['scopes']) != set(SCOPES.values()):
        raise ValueError('Phase 1 requires exactly the three bridge scopes')
    validate_workspaces(config['workspaces'])
    pinned_sessions(config)
    return config


def validate_workspaces(workspaces):
    if not isinstance(workspaces, dict) or not workspaces:
        raise ValueError('At least one approved workspace alias is required')
    for alias, w in workspaces.items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', alias) or set(w) != {'cwd','root','project','ide'}:
            raise ValueError('Invalid workspace configuration')
        broker.identifier(w['project'])
        if w['ide'] not in sessions.CLAUDE_IDES:
            raise ValueError('Unsupported IDE')
        for key in ('cwd','root'):
            p = Path(w[key])
            if not p.is_absolute() or not p.is_dir() or str(p.resolve()) != str(p):
                raise ValueError('Workspace paths must be existing canonical absolute directories')
        if not Path(w['cwd']).is_relative_to(w['root']):
            raise ValueError('Workspace is outside its allowed root')


def pinned_sessions(config):
    """Explicit grants, never inferred ownership or arbitrary route adoption."""
    grants = config.get('pinned_sessions', {})
    if not isinstance(grants, dict):
        raise ValueError('Pinned sessions must be a mapping')
    result = {}
    for alias, g in grants.items():
        if (not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', alias)
                or alias in config['workspaces']
                or set(g) != {'uuid','cwd','project','route','authorized_at'}):
            raise ValueError('Invalid pinned session grant')
        if str(uuid.UUID(g['uuid'])) != g['uuid']:
            raise ValueError('Canonical session UUID required')
        cwd = Path(g['cwd'])
        if not cwd.is_absolute() or not cwd.is_dir() or str(cwd.resolve()) != str(cwd):
            raise ValueError('Canonical existing pinned cwd required')
        broker.identifier(g['project']); broker.identifier(g['route'])
        datetime.fromisoformat(g['authorized_at'])
        identity = digest([config['principal'], config['tunnel_id'], alias, g])
        sid = 'as_' + identity[:32]
        result[sid] = {'id':sid, 'uuid':g['uuid'], 'alias':alias,
            'workspace':{'cwd':g['cwd'], 'root':g['cwd'], 'project':g['project'], 'ide':'antigravity-ide'},
            'route':g['route'], 'sender':'mcp-' + identity[32:],
            'created_at':g['authorized_at'], 'last_activity_at':g['authorized_at'],
            'principal':config['principal'], 'registered':True, 'pinned':True}
    return result


class Backend:
    def __init__(self, path):
        self.path = path

    def rpc(self, op, **args):
        return broker.request(self.path, {'op':op, **args}, timeout=3)

    def instance(self):
        value = broker.request(self.path, {'op':'ping'}, timeout=.5)['instance']
        if not isinstance(value, str) or not value:
            raise ValueError('Invalid instance')
        return value

    def create(self, workspace):
        # Same policy and supported IDE opening path as bridge register --new.
        from types import SimpleNamespace
        args = SimpleNamespace(socket=self.path, model=None)
        bridge.new_session_policy(args, 'claude')
        row = sessions.open_session('claude', cwd=workspace['cwd'], host='ide',
                                    ide=workspace['ide'], timeout=180)['session']
        sid = str(uuid.UUID(row['id']))
        # Return the acquired UUID immediately; registration rechecks exact cwd after it is durable.
        return sid

    def check(self, session):
        row = sessions.resolve_claude_peer(session['uuid'])
        if row['id'] != session['uuid'] or Path(row['cwd']).resolve() != Path(session['workspace']['cwd']):
            raise ValueError('Session identity changed')
        if session.get('pinned'):
            rows = self.rpc('routes', project=session['workspace']['project'])
            route = next((r for r in rows if r['name'] == session['route']), {})
            if (route.get('transport'),route.get('session'),route.get('cwd')) != (
                    'claude-peer',session['uuid'],session['workspace']['cwd']):
                raise ValueError('Pinned route changed')
        return row

    def availability(self, session):
        try:
            status = self.check(session).get('status')
            return 'ready' if status in ('idle','ready') else 'busy' if status in ('running','busy') else 'unavailable'
        except (ValueError, OSError):
            return 'unavailable'

    def register(self, session):
        self.check(session)
        w = session['workspace']
        expected = {'project':w['project'], 'name':session['route'], 'cwd':w['cwd'],
                    'transport':'claude-peer','session':session['uuid']}
        sender = dict(project=w['project'], name=session['sender'], cwd=w['cwd'], transport='realtime',session=None)
        rows = self.rpc('routes', project=w['project'])
        for fields in (expected, sender):
            existing = next((r for r in rows if r['name'] == fields['name']), None)
            if existing is not None:
                if any(existing.get(k) != v for k,v in fields.items()):
                    raise ValueError('Route collision')
            else:
                if session.get('pinned') and fields is expected:
                    raise ValueError('Pinned target route missing')
                self.rpc('register', **fields)
        self.verify_routes(session)

    def verify_routes(self, session):
        self.check(session)
        rows = self.rpc('routes', project=session['workspace']['project'])
        for name, transport, sid in ((session['route'],'claude-peer',session['uuid']),
                                      (session['sender'],'realtime',None)):
            row = next((r for r in rows if r['name'] == name), {})
            if (row.get('transport'),row.get('session'),row.get('cwd')) != (transport,sid,session['workspace']['cwd']):
                raise ValueError('Route changed')

    def post(self, op, session):
        if session.get('pinned'):
            self.register(session)  # Only the private return route may be created.
        self.verify_routes(session)
        m = self.rpc('send', project=session['workspace']['project'],sender=session['sender'],
                     recipient=session['route'],body=op['message'],key=op.get('downstream_key',op['key_hash']))
        # Validate broker snapshot before handing it to push; never route by name again.
        if m['route']['session'] != session['uuid'] or m['route']['transport'] != 'claude-peer':
            raise ValueError('Unexpected message route')
        return m

    def push(self, message, session):
        self.check(session)
        return bridge.push_if_peer(self.path, message)

    def observe(self, mid):
        m = self.rpc('show', id=mid)
        if m.get('id') != mid:
            raise ValueError('Unexpected message identifier')
        replies = self.rpc('wait', id=mid, timeout=0.01)['replies']
        return m, [r for r in replies if r.get('reply_to') == mid and isinstance(r.get('body'),str) and r['body'].strip()]

    def retire_reply(self, mid, parent):
        try:
            reply = self.rpc('show', id=mid)
        except ValueError as error:
            if str(error) == 'Unknown message; broker may have restarted':
                return
            raise
        if reply.get('reply_to') != parent:
            raise ValueError('Reply correlation changed')
        if reply.get('state') in ('done','answered','failed'):
            return
        if reply.get('state') not in ('pending','received','in_progress'):
            raise ValueError('Reply delivery remains uncertain')
        self.rpc('resolve', id=mid, state='done', note='MCP result durably captured')


class Engine:
    def __init__(self, config, backend, start=True):
        validate_workspaces(config['workspaces'])
        pinned_sessions(config)
        self.config, self.backend = config, backend
        self.principal = config['principal']
        self.scopes = frozenset(config['scopes'])
        if config.get('writes_enabled') is not True or not config.get('principal') or not config.get('tunnel_id'):
            raise ValueError('Explicit owner and write approval configuration required')
        self.root = Path(config['state_dir'])
        if not self.root.is_absolute() or self.root.resolve() != self.root:
            raise ValueError('State directory must be canonical and absolute')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        s = self.root.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
            raise ValueError('Private owner-only state directory required')
        self.lock_fd = private_file(self.root/'worker.lock')
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            raise ValueError('A worker already owns this ledger') from None
        os.close(private_file(self.root/'ledger.sqlite3'))
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root/'ledger.sqlite3', check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, principal TEXT NOT NULL,
            tool TEXT NOT NULL, key_hash TEXT NOT NULL, payload_hash TEXT NOT NULL, doc TEXT NOT NULL,
            UNIQUE(principal,tool,key_hash));
          CREATE TABLE IF NOT EXISTS owned_sessions (id TEXT PRIMARY KEY, principal TEXT NOT NULL, doc TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        binding = digest([config['principal'],config['tunnel_id']])
        self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',('owner_binding',binding))
        if self.db.execute('SELECT value FROM metadata WHERE key=?',('owner_binding',)).fetchone()[0] != binding:
            self.db.close(); os.close(self.lock_fd)
            raise ValueError('Ledger belongs to a different private tunnel owner')
        self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',('cursor_key',secrets.token_hex(32)))
        self.cursor_key = bytes.fromhex(self.db.execute('SELECT value FROM metadata WHERE key=?',('cursor_key',)).fetchone()[0])
        self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',('ledger_namespace',secrets.token_hex(32)))
        self.ledger_namespace = self.db.execute('SELECT value FROM metadata WHERE key=?',('ledger_namespace',)).fetchone()[0]
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.recover()
        if start:
            self.thread = threading.Thread(target=self.run, daemon=True, name='bridge-mcp-worker')
            self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread:
            self.thread.join(timeout=195)
            if self.thread.is_alive():
                raise RuntimeError('Worker still active; ledger remains locked')
        self.db.close(); os.close(self.lock_fd)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def rows(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT doc FROM operations')]

    def save(self, op):
        op['updated_at'] = max(op['updated_at'], stamp())
        if op['status'] in ('succeeded','failed'):
            op.setdefault('finished_epoch',time.time())
        self.db.execute('UPDATE operations SET doc=? WHERE id=?',(json.dumps(op),op['operation_id']))

    def update(self, op, **fields):
        with self.transaction():
            op.update(fields); self.save(op)

    def session(self, sid, principal):
        if principal == self.principal and sid in pinned_sessions(self.config):
            return pinned_sessions(self.config)[sid]
        with self.lock:
            row = self.db.execute('SELECT doc FROM owned_sessions WHERE id=? AND principal=?',(sid,principal)).fetchone()
        if row is None:
            raise Fault('NOT_FOUND')
        return json.loads(row[0])

    def operation(self, oid, principal):
        with self.lock:
            row = self.db.execute('SELECT doc FROM operations WHERE id=? AND principal=?',(oid,principal)).fetchone()
        if row is None:
            raise Fault('NOT_FOUND')
        op = json.loads(row[0])
        if op.get('expired'):
            raise Fault('OPERATION_EXPIRED')
        return op

    def authorized(self, name, principal):
        if principal != self.principal or SCOPES.get(name) not in self.scopes:
            raise Fault('NOT_AUTHORIZED')

    def tools(self):
        return [t for t in TOOLS if SCOPES[t['name']] in self.scopes]

    def validate(self, name, args):
        if not isinstance(args, dict):
            raise Fault('INVALID_ARGUMENT')
        schema = next(t['inputSchema'] for t in TOOLS if t['name']==name)
        if set(args)-set(schema['properties']) or set(schema.get('required',[]))-set(args):
            raise Fault('INVALID_ARGUMENT')
        for k,v in args.items():
            p = schema['properties'][k]
            if p['type'] == 'integer':
                valid = type(v) is int and p['minimum'] <= v <= p['maximum']
            else:
                valid = isinstance(v,str) and p.get('minLength',0) <= len(v) <= p.get('maxLength',100000)
                if valid and 'pattern' in p:
                    valid = re.fullmatch(p['pattern'],v) is not None
            if not valid:
                raise Fault('INVALID_ARGUMENT')
        if 'message' in args:
            try:
                valid = args['message'].strip() and len(args['message'].encode('utf-8')) <= 32768
            except UnicodeEncodeError:
                valid = False
            if not valid:
                raise Fault('INVALID_ARGUMENT')

    def call(self, name, args, principal=None):
        principal = self.principal if principal is None else principal
        try:
            self.authorized(name,principal); self.validate(name,args)
            if name in ('bridge_session_create','bridge_message_send'):
                data = self.accept(name,args,principal)
            elif name == 'bridge_sessions_list':
                data = self.list_sessions(args,principal)
            elif name == 'bridge_operation_get':
                data = self.get(args,principal)
            else:
                raise Fault('INVALID_ARGUMENT')
            return {'ok':True,'data':data}
        except Fault as e:
            return {'ok':False,'error':e.payload}
        except (sqlite3.Error, OSError):
            return {'ok':False,'error':Fault('CAPACITY_EXCEEDED').payload}
        except Exception:
            return {'ok':False,'error':Fault('INTERNAL_ERROR').payload}

    def accepted(self, op, replayed):
        result = {k:op[k] for k in ('operation_id','kind','accepted_at')}
        result['replayed'] = replayed
        if op['message_request_id']:
            result['message_request_id'] = op['message_request_id']
        return result

    def accept(self, tool, args, principal):
        creating = tool == 'bridge_session_create'
        if creating:
            if args['workspace_alias'] not in self.config['workspaces']:
                raise Fault('WORKSPACE_NOT_ALLOWED')
            payload = {'contract_version':'0.1.0','workspace_alias':args['workspace_alias'],'agent_type':'claude'}
        else:
            session = self.session(args['agent_session_id'],principal)
            self.allowed(session)
            payload = {'contract_version':'0.1.0','agent_session_id':args['agent_session_id'],'message':args['message']}
        key_hash = digest([principal,tool,args['idempotency_key']]); payload_hash = digest(payload)
        with self.transaction():
            row = self.db.execute('SELECT doc,payload_hash FROM operations WHERE principal=? AND tool=? AND key_hash=?',
                                   (principal,tool,key_hash)).fetchone()
            if row:
                if row[1] != payload_hash:
                    raise Fault('IDEMPOTENCY_CONFLICT')
                op = json.loads(row[0])
                if op.get('expired'):
                    raise Fault('IDEMPOTENCY_EXPIRED')
                return self.accepted(op, True)
            if self.stop.is_set():
                raise Fault('BACKEND_UNAVAILABLE')
            ops = self.rows()
            if not creating and any(o['agent_session_id']==args['agent_session_id'] and o['status'] in ACTIVE
                                    and o['kind']=='message_send' for o in ops):
                raise Fault('SESSION_BUSY')
            if sum(o['status'] in ACTIVE for o in ops) >= 64 or (self.root/'ledger.sqlite3').stat().st_size >= 64*1024*1024:
                raise Fault('CAPACITY_EXCEEDED')
            try:
                self.backend.instance()
                if not creating and (not session.get('registered') or self.backend.availability(session) not in (('ready','busy') if session.get('pinned') else ('ready',))):
                    raise ValueError('Unavailable dedicated session')
            except Exception:
                raise Fault('BACKEND_UNAVAILABLE') from None
            when = stamp()
            op = {'operation_id':opaque('op_'),'kind':'session_create' if creating else 'message_send',
                  'status':'accepted','accepted_at':when,'updated_at':when,'accepted_epoch':time.time(),
                  'agent_session_id':None if creating else args['agent_session_id'],
                  'message_request_id':None if creating else opaque('msg_'),'backend_message_state':None,
                  'result':None,'failure':None,'stage':'queued','principal':principal,'key_hash':key_hash,
                  'instance':None,'backend_id':None,
                  'downstream_key':digest([self.ledger_namespace,key_hash])}
            if creating:
                op.update(alias=args['workspace_alias'],workspace=dict(self.config['workspaces'][args['workspace_alias']]))
            else:
                op['message'] = args['message']
            self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                            (op['operation_id'],principal,tool,key_hash,payload_hash,json.dumps(op)))
        self.wake.set()
        return self.accepted(op,False)

    def allowed(self, session):
        if session.get('pinned'):
            if pinned_sessions(self.config).get(session['id']) != session:
                raise Fault('NOT_AUTHORIZED')
            return
        # An alias remap cannot silently retarget a previously accepted operation.
        if self.config['workspaces'].get(session['alias']) != session['workspace']:
            raise Fault('WORKSPACE_NOT_ALLOWED')
        validate_workspaces({session['alias']:session['workspace']})

    def fail(self, op, code='DELIVERY_UNKNOWN', unknown=True):
        failure = Fault(code).payload
        failure['operation_id'] = op['operation_id']
        self.update(op,status='unknown' if unknown else 'failed',failure=failure,result=None)

    def recover(self):
        for op in self.rows():
            if op['status'] == 'running':
                # Known IDs may be observed; no create/post/push call is replayed.
                if op['kind'] == 'message_send' and op['backend_id']:
                    self.observe(op, recovering=True)
                else:
                    self.fail(op)

    def run(self):
        while not self.stop.is_set():
            self.tick()
            self.wake.wait(.2); self.wake.clear()

    def tick(self):
        for op in self.rows():
            if self.stop.is_set():
                return
            try:
                if op['status']=='accepted':
                    self.dispatch(op)
                elif op['status']=='running' and op['kind']=='message_send':
                    self.observe(op)
                elif op['status']=='succeeded' and op.get('pending_reply_cleanup'):
                    self.cleanup_replies(op)
                elif op['status'] in ('succeeded','failed') and not op.get('expired') and time.time()-op.get('finished_epoch',time.time()) >= RETENTION:
                    self.update(op,expired=True,message=None,result=None,text=None)
            except Exception:
                # Persist conservatively; if disk fails, stop the worker without further effects.
                try:
                    self.fail(op)
                except Exception:
                    self.stop.set(); return

    def dispatch(self, op):
        try:
            instance = self.backend.instance()
            if op['kind']=='session_create':
                self.allowed({'alias':op['alias'],'workspace':op['workspace']})
            else:
                session = self.session(op['agent_session_id'],op['principal']); self.allowed(session)
                if self.backend.availability(session) not in (('ready','busy') if session.get('pinned') else ('ready',)):
                    raise Fault('BACKEND_UNAVAILABLE')
        except Exception:
            self.fail(op,'BACKEND_UNAVAILABLE',unknown=False); return
        if op['kind']=='session_create':
            # All facade ledgers on this host share one creation lock. Nonblocking: leave queued.
            fd = private_file(Path(tempfile.gettempdir())/f'agent-bridge-mcp-create-{os.getuid()}.lock')
            try:
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                self.update(op,status='running',stage='create_intent',instance=instance)
                sid = self.backend.create(op['workspace'])
                self.update(op,stage='created',backend_session=sid)
                session = {'id':opaque('as_'),'uuid':sid,'alias':op['alias'],'workspace':op['workspace'],
                           'route':'mcp-'+secrets.token_hex(16),'sender':'mcp-'+secrets.token_hex(16),
                           'created_at':stamp(),'last_activity_at':stamp(),'principal':op['principal'],'registered':False}
                with self.transaction():
                    self.db.execute('INSERT INTO owned_sessions VALUES (?,?,?)',(session['id'],op['principal'],json.dumps(session)))
                    op.update(agent_session_id=session['id'],stage='register_intent');self.save(op)
                self.backend.register(session)
                self.registered(op,session)
            finally:
                os.close(fd)
        else:
            self.update(op,status='running',stage='post_intent',instance=instance)
            message = self.backend.post(op,session)
            self.update(op,stage='posted',backend_id=message['id'],backend_message_state=message['state'])
            if self.backend.instance() != instance:
                self.fail(op); return
            self.update(op,stage='push_intent')
            try:
                message = self.backend.push(message,session)
            except bridge.PushFailed as e:
                # A recorded pre-send failure is definite; uncertain stays blocked.
                m = e.message
                self.update(op,backend_message_state=m['state'])
                self.fail(op,'BACKEND_UNAVAILABLE' if m['state']=='pending' else 'DELIVERY_UNKNOWN',m['state']!='pending')
                return
            self.update(op,stage='observing',backend_message_state=message['state'])
            with self.transaction():
                session['last_activity_at'] = stamp()
                self.db.execute('UPDATE owned_sessions SET doc=? WHERE id=?',(json.dumps(session),session['id']))
            self.observe(op)

    def registered(self, op, session):
        with self.transaction():
            session['registered'] = True
            self.db.execute('UPDATE owned_sessions SET doc=? WHERE id=?',(json.dumps(session),session['id']))
            op.update(stage='registered',status='succeeded',failure=None,
                      result={'type':'session_created','agent_session_id':session['id']})
            self.save(op)

    def recover_registration(self, oid):
        # Explicit operator action, never remote or automatic. Only rebind a known exact UUID.
        op = self.operation(oid,self.principal)
        if op['kind']!='session_create' or op['status']!='unknown' or not op.get('backend_session'):
            raise Fault('INVALID_ARGUMENT')
        if not op['agent_session_id']:
            self.allowed({'alias':op['alias'],'workspace':op['workspace']})
            session = {'id':opaque('as_'),'uuid':op['backend_session'],'alias':op['alias'],'workspace':op['workspace'],
                       'route':'mcp-'+secrets.token_hex(16),'sender':'mcp-'+secrets.token_hex(16),
                       'created_at':stamp(),'last_activity_at':stamp(),'principal':op['principal'],'registered':False}
            with self.transaction():
                self.db.execute('INSERT INTO owned_sessions VALUES (?,?,?)',(session['id'],op['principal'],json.dumps(session)))
                op['agent_session_id'] = session['id']; self.save(op)
        session = self.session(op['agent_session_id'],self.principal)
        self.allowed(session)
        if session['uuid'] != op['backend_session']:
            raise Fault('INVALID_ARGUMENT')
        self.backend.register(session)
        self.registered(op,session)
        return self.get({'operation_id':oid},self.principal)

    def observe(self, op, recovering=False):
        try:
            if self.backend.instance()!=op['instance']:
                self.fail(op); return
            message,replies = self.backend.observe(op['backend_id'])
            state = message['state']
            if state not in STATES:
                raise ValueError('Unknown message state')
            self.update(op,backend_message_state=state)
            replies = [r for r in replies if r.get('reply_to') == op['backend_id'] and isinstance(r.get('body'),str) and r['body'].strip()]
            if replies:
                text = '\n\n'.join(r['body'] for r in sorted(replies,key=lambda r:(r['created'],r['id'])))
                if len(text.encode()) > MAX_RESULT:
                    self.fail(op,'CAPACITY_EXCEEDED',False); return
                self.update(op,status='succeeded',stage='complete',text=text,result={'type':'message_result'},
                            pending_reply_cleanup=[r['id'] for r in replies])
                self.cleanup_replies(op)
            elif state=='failed':
                self.fail(op,'BACKEND_UNAVAILABLE',False)
            elif state=='uncertain' or (recovering and state in ('pending','dispatching')):
                self.fail(op)
        except Exception:
            self.fail(op)

    def cleanup_replies(self, op):
        # Only after the result commit. Cleanup retries never replay message effects.
        try:
            if self.backend.instance() != op['instance']:
                self.update(op,pending_reply_cleanup=[])
                return
            remaining = list(op.get('pending_reply_cleanup',[]))
            for mid in list(remaining):
                self.backend.retire_reply(mid,op['backend_id'])
                remaining.remove(mid)
                self.update(op,pending_reply_cleanup=list(remaining))
        except Exception:
            # Keep durable success and remaining IDs through outages or lost resolve replies.
            return

    def cursor(self, principal, kind, obj, offset):
        payload = json.dumps([principal,kind,obj,offset,int(time.time())+3600],separators=(',',':')).encode()
        return base64.urlsafe_b64encode(payload+hmac.digest(self.cursor_key,payload,'sha256')).decode().rstrip('=')

    def offset(self, token, principal, kind, obj):
        try:
            data = base64.urlsafe_b64decode(token+'='*(-len(token)%4)); raw,signature = data[:-32],data[-32:]
            if not hmac.compare_digest(signature,hmac.digest(self.cursor_key,raw,'sha256')):
                raise ValueError()
            p,k,o,offset,expiry = json.loads(raw)
            if (p,k,o)!=(principal,kind,obj) or expiry < time.time():
                raise ValueError()
            return offset
        except Exception:
            raise Fault('INVALID_ARGUMENT') from None

    def list_sessions(self, args, principal):
        last = self.offset(args['cursor'],principal,'sessions','') if 'cursor' in args else ''
        with self.lock:
            rows = self.db.execute('SELECT doc FROM owned_sessions WHERE principal=? AND id>? ORDER BY id LIMIT ?',
                                    (principal,last,args.get('limit',20)+1)).fetchall()
        owned = [json.loads(r[0]) for r in rows]
        if principal == self.principal:
            owned += [s for s in pinned_sessions(self.config).values() if s['id'] > last]
        owned.sort(key=lambda s:s['id'])
        more = len(owned)>args.get('limit',20)
        owned = owned[:args.get('limit',20)]; result = []
        ops = self.rows()
        for s in owned:
            try:
                self.allowed(s)
                if not s.get('registered'):
                    raise Fault('BACKEND_UNAVAILABLE')
                state = 'busy' if any(o['agent_session_id']==s['id'] and o['status'] in ACTIVE for o in ops) else self.backend.availability(s)
            except Exception:
                state = 'unavailable'
            result.append({'agent_session_id':s['id'],'agent_type':'claude','workspace_alias':s['alias'],
                           'state':state,'created_at':s['created_at'],'last_activity_at':s['last_activity_at']})
        return {'sessions':result,'has_more':more,'next_cursor':self.cursor(principal,'sessions','',owned[-1]['id']) if more else None}

    def get(self, args, principal):
        deadline = time.monotonic()+args.get('wait_ms',0)/1000
        while True:
            op = self.operation(args['operation_id'],principal)
            if 'cursor' in args or op['status'] not in ('accepted','running') or time.monotonic()>=deadline:
                break
            time.sleep(min(.02,max(0,deadline-time.monotonic())))
        output = {k:op[k] for k in ('operation_id','kind','status','accepted_at','updated_at','agent_session_id',
                                   'message_request_id','backend_message_state','result','failure')}
        if 'cursor' in args and not (op['status']=='succeeded' and op['kind']=='message_send'):
            raise Fault('INVALID_ARGUMENT')
        if op['status']=='succeeded' and op['kind']=='message_send':
            offset = self.offset(args['cursor'],principal,'result',op['operation_id']) if 'cursor' in args else 0
            end = offset+args.get('max_chars',8000); text = op['text']; more = end<len(text)
            output['result'] = {'type':'message_result','text':text[offset:end],'has_more':more,
                                'next_cursor':self.cursor(principal,'result',op['operation_id'],end) if more else None}
        return output
