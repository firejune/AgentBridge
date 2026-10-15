"""Volatile single-process broker, Unix socket RPC and event subscriptions."""
import asyncio
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import uuid

DEFAULT_SOCKET = Path.home() / '.local/share/agent-bridge/bridge.sock'
MAX_FRAME = 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_-]*',value):
        raise ValueError('Use a lowercase identifier')
    return value


def wire(value):
    data = (json.dumps(value,ensure_ascii=False)+'\n').encode()
    if len(data) > MAX_FRAME:
        raise ValueError('Response exceeds frame limit; use a smaller query')
    return data


def route_key(route):
    return tuple(route.get(k) for k in ('project','name','transport','session','cwd','channel','generation'))


class State:
    def __init__(self, max_messages=2048):
        self.instance = str(uuid.uuid4())
        self.max_messages = max_messages
        self.endpoints, self.channels, self.messages, self.keys = {}, {}, {}, {}
        self.events = deque(maxlen=1024)
        self.sequence = 0
        self.subscribers = []
        self.answers = {}

    def get(self, mid):
        if mid not in self.messages:
            raise ValueError('Unknown message; broker may have restarted')
        return self.messages[mid]

    def route(self, project, name=None, channel=None):
        if channel:
            item = self.channels.get((project,channel))
            if not item or not item['active']:
                raise ValueError('Channel is missing or closed')
            return deepcopy(item['route'])
        item = self.endpoints.get((project,name))
        if not item:
            raise ValueError('Unregistered endpoint')
        return deepcopy(item)

    def matches(self, sub, event):
        m = event['message']
        destinations = [m['route']]
        if event['type'] == 'status':
            destinations.append(m['return_route'])
        return any(route_key(route) == sub['route'] for route in destinations)

    def publish(self, m, kind):
        self.sequence += 1
        event = {'seq':self.sequence,'instance':self.instance,'type':kind,'message':deepcopy(m)}
        self.events.append(event)
        for sub in self.subscribers:
            if sub['overflow']:
                continue
            if self.matches(sub,event):
                try:
                    sub['queue'].put_nowait(event)
                except asyncio.QueueFull:
                    # Disconnect visibly instead of silently dropping messages.
                    while not sub['queue'].empty():
                        sub['queue'].get_nowait()
                    sub['queue'].put_nowait({'error':'Subscriber fell behind; reconnect and inspect inbox'})
                    sub['overflow'] = True

    def status(self, m, state, note=None, event=None):
        m.update(state=state,note=note)
        entry = {'state':state,'note':note,'at':now()}
        if event:
            entry['event'] = event
        m['events'].append(entry)
        m['events'] = m['events'][-100:]
        self.publish(m,'status')
        return m

    def post(self, args, reply_to=None):
        body, key = args['body'], args['key']
        if not isinstance(body,str) or not body.strip() or len(body.encode()) > 32768:
            raise ValueError('Message must contain 1–32768 UTF-8 bytes')
        if not isinstance(key,str) or not key.strip() or len(key)>256:
            raise ValueError('Provide an idempotency key of at most 256 characters')
        intent = {k:args.get(k) for k in ('project','sender','recipient','channel','sender_channel','body','key')}
        intent['reply_to'] = reply_to
        if key in self.keys:
            original_intent, mid = self.keys[key]
            if original_intent != intent:
                raise ValueError('Idempotency key already used with different content')
            return self.get(mid)
        if len(self.messages) >= self.max_messages:
            raise ValueError('In-memory message limit reached; finish work and restart broker')
        if reply_to:
            parent = self.get(reply_to)
            destination, origin = deepcopy(parent['return_route']), deepcopy(parent['route'])
            conversation = parent['conversation']
        else:
            destination = self.route(args['project'],args.get('recipient'),args.get('channel'))
            origin = self.route(args['project'],args.get('sender'),args.get('sender_channel'))
            if route_key(destination)==route_key(origin):
                raise ValueError('Sender and recipient addresses must differ')
            conversation = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        m = {'id':mid,'conversation':conversation,'project':destination['project'],
             'sender':origin['name'],'recipient':destination['name'],'route':destination,
             'return_route':origin,'channel':destination.get('channel'),
             'sender_channel':origin.get('channel'),'body':body,'reply_to':reply_to,
             'state':'pending','created':now(),'events':[],'note':None}
        self.messages[mid] = m
        self.keys[key] = (intent,mid)
        self.answers[mid] = asyncio.Event()
        self.publish(m,'message')
        if reply_to:
            self.status(parent,'answered',mid)
            self.answers[reply_to].set()
        return m

    def action(self, args):
        op = args['op']
        if op == 'ping':
            return {'instance':self.instance,'messages':len(self.messages),'subscribers':len(self.subscribers)}
        if op == 'register':
            project, name = identifier(args['project']), identifier(args['name'])
            transport = args.get('transport','realtime')
            if transport not in ('realtime','codex','claude','claude-peer','agy'):
                raise ValueError('Unknown transport')
            cwd = str(Path(args['cwd']).expanduser().resolve(strict=True))
            if not Path(cwd).is_dir():
                raise ValueError('Working directory must be a directory')
            sid = args.get('session')
            if transport != 'realtime' or sid:
                sid = str(uuid.UUID(sid or ''))
            item = dict(project=project,name=name,transport=transport,cwd=cwd,
                        session=sid,executable=args.get('executable'))
            self.endpoints[project,name] = item
            return item
        if op == 'routes':
            return [x for (p,_),x in self.endpoints.items() if p==args['project']]
        if op == 'channel-bind':
            project, name = args['project'], identifier(args['name'])
            existing = self.channels.get((project,name))
            generation = existing['generation'] if existing else 0
            if args.get('expect_generation',0) != generation:
                raise ValueError(f'Channel generation changed; current generation is {generation}')
            if not args['description'].strip():
                raise ValueError('Channel description is required')
            route = self.route(project,args['owner'])
            route.update(channel=name,generation=generation+1)
            item = dict(project=project,name=name,owner=args['owner'],description=args['description'],
                        route=route,generation=generation+1,active=True)
            self.channels[project,name] = item
            return item
        if op == 'channels':
            result = []
            for (project,_),item in self.channels.items():
                if project==args['project']:
                    result.append({**item,'subscribers':sum(sub['route']==route_key(item['route']) for sub in self.subscribers)})
            return result
        if op == 'channel-close':
            item = self.channels.get((args['project'],args['name']))
            if not item or item['generation']!=args['expect_generation']:
                raise ValueError('Channel missing or generation changed')
            item.update(active=False,generation=item['generation']+1)
            return item
        if op in ('send','reply'):
            return self.post(args,args.get('id') if op=='reply' else None)
        if op == 'show':
            return self.get(args['id'])
        if op == 'conversation':
            cid = self.get(args['id'])['conversation']
            return [m for m in self.messages.values() if m['conversation']==cid]
        if op == 'inbox':
            if not args.get('name') and not args.get('channel'):
                raise ValueError('Select a recipient or channel')
            return [{k:m[k] for k in ('id','sender','recipient','state','channel','sender_channel','reply_to','created')}
                    | {'session':m['route']['session'],'generation':m['route'].get('generation')}
                    for m in self.messages.values() if m['project']==args['project']
                    and (not args.get('name') or m['recipient']==args['name'])
                    and (not args.get('channel') or m['channel']==args['channel'])
                    and (not args.get('session') or m['route']['session']==args['session'])
                    and (args.get('all') or m['state'] not in ('done','answered'))]
        if op in ('ack','progress','resolve','claim','finish'):
            m = self.get(args['id'])
            if op=='claim':
                if m['state']!='pending':
                    raise ValueError('Only pending messages may be delivered')
                destination = (m['route']['transport'],m['route']['session'])
                for other in self.messages.values():
                    if other['state'] in ('dispatching','uncertain') and (
                            other['route']['transport'],other['route']['session'])==destination:
                        raise ValueError('Recipient has an active or uncertain delivery')
                return self.status(m,'dispatching')
            if op=='finish':
                if m['state']!='dispatching':
                    return m
                if args['state'] not in ('submitted','failed','uncertain','pending'):
                    raise ValueError('Invalid delivery result')
                event = args.get('event')
                if event not in (None,'pushed','push_failed'):
                    raise ValueError('Invalid delivery event')
                if args['state']=='pending' and (event!='push_failed' or not args.get('note','').strip()):
                    # A failed push stays deliverable, but never silently.
                    raise ValueError('Returning to pending requires a push_failed event and reason')
                return self.status(m,args['state'],args.get('note'),event)
            if m['state'] in ('done','answered'):
                raise ValueError('Message is already complete')
            if op in ('ack','progress') and m['state'] in ('dispatching','uncertain'):
                raise ValueError('Resolve delivery outcome first')
            state = {'ack':'received','progress':'in_progress'}.get(op,args.get('state'))
            if op=='resolve' and (state not in ('pending','submitted','done') or not args.get('note','').strip()):
                raise ValueError('Recovery requires a supported state and explanation')
            return self.status(m,state,args.get('note'))
        raise ValueError('Unknown operation')


async def write(writer, payload):
    writer.write(wire(payload))
    await asyncio.wait_for(writer.drain(),5)


async def wait_answer(state, args, reader):
    mid = args['id']
    state.get(mid)
    timeout = args.get('timeout',60)
    if not 0 < timeout <= 3600:
        raise ValueError('Timeout must be between 0 and 3600 seconds')
    pending = [asyncio.create_task(state.answers[mid].wait()),asyncio.create_task(reader.read(1))]
    try:
        done,_ = await asyncio.wait(pending,timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
        if pending[1] in done:
            return None
        replies = [m for m in state.messages.values() if m['reply_to']==mid]
        return {'id':mid,'timed_out':not bool(replies),'replies':replies}
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)


async def watch(state, args, reader, writer):
    if len(state.subscribers)>=256:
        raise ValueError('Subscriber limit reached')
    route = state.route(args['project'],args.get('name'),args.get('channel'))
    cursor = args.get('after')
    if cursor is not None:
        if args.get('instance') != state.instance:
            raise ValueError('Broker instance changed; register and resynchronize')
        if cursor>state.sequence or (state.events and cursor<state.events[0]['seq']-1):
            raise ValueError('Event cursor is outside retained history; inspect inbox')
    sub = {'route':route_key(route),'queue':asyncio.Queue(maxsize=128),'overflow':False}
    backlog = [event for event in state.events if cursor is not None and event['seq']>cursor and state.matches(sub,event)]
    state.subscribers.append(sub)  # Register before yielding to avoid a subscribe/send race.
    disconnected = asyncio.create_task(reader.read(1))
    try:
        await write(writer,{'type':'subscribed','instance':state.instance,'seq':state.sequence,'route':route})
        for event in backlog:
            await write(writer,event)
        while True:
            next_event = asyncio.create_task(sub['queue'].get())
            try:
                done,_ = await asyncio.wait([disconnected,next_event],return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    return
                event = next_event.result()
                await write(writer,event)
                if 'error' in event:
                    return
            finally:
                next_event.cancel()
                await asyncio.gather(next_event,return_exceptions=True)
    finally:
        state.subscribers.remove(sub)
        disconnected.cancel()
        await asyncio.gather(disconnected,return_exceptions=True)


async def serve(path, max_messages=2048):
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock = path.with_suffix('.lock').open('a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise ValueError('Broker is already running for this socket') from error
    server = None
    try:
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise ValueError('Refusing to replace a non-socket file')
            path.unlink()
        state = State(max_messages)
        stopping = asyncio.Event()
        handlers = set()
        async def handle(reader,writer):
            handlers.add(asyncio.current_task())
            try:
                raw = await asyncio.wait_for(reader.readline(),10)
                args = json.loads(raw)
                if not isinstance(args,dict):
                    raise ValueError('Request must be an object')
                op = args.get('op')
                if op=='watch':
                    await watch(state,args,reader,writer)
                elif op=='wait':
                    result = await wait_answer(state,args,reader)
                    if result is not None:
                        await write(writer,{'result':result,'instance':state.instance})
                elif op=='shutdown':
                    await write(writer,{'result':{'stopping':True},'instance':state.instance})
                    stopping.set()
                else:
                    result = state.action(args)  # No await inside mutation: one atomic event-loop turn.
                    await write(writer,{'result':result,'instance':state.instance})
            except (ValueError,KeyError,TypeError,OSError,asyncio.TimeoutError) as error:
                try:
                    await write(writer,{'error':str(error),'instance':state.instance})
                except (OSError,asyncio.TimeoutError):
                    pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                handlers.discard(asyncio.current_task())
        server = await asyncio.start_unix_server(handle,path=str(path),limit=MAX_FRAME)
        os.chmod(path,0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM):
            loop.add_signal_handler(sig,stopping.set)
        print(json.dumps({'ready':True,'socket':str(path),'instance':state.instance}),flush=True)
        await stopping.wait()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers,return_exceptions=True)
    finally:
        if server is not None:
            server.close()
            path.unlink(missing_ok=True)
        lock.close()


def stream(path, args, timeout=None):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(Path(path).expanduser()))
        except OSError as error:
            raise ValueError('Broker unavailable; start bridge.py serve for this socket') from error
        sock.sendall(wire(args))
        with sock.makefile('rb') as reader:
            while True:
                line = reader.readline(MAX_FRAME+1)
                if not line:
                    raise ValueError('Broker disconnected; delivery outcome may be unknown')
                if len(line)>MAX_FRAME:
                    raise ValueError('Broker frame exceeds limit')
                result = json.loads(line)
                if 'error' in result:
                    raise ValueError(result['error'])
                yield result


def request(path, args, timeout=10):
    connection = stream(path,args,timeout)
    try:
        return next(connection)['result']
    finally:
        connection.close()
