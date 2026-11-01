#!/usr/bin/env python3
"""MCP facade, phase 0: one read-only tool, bridge_status, over stdio or loopback HTTP.

Contract: bridge MCP spec 0.1.0. Phase 0 (connectivity_only) lists and runs bridge_status
only; every other tool name is an unknown tool, so a mutation cannot be called even
directly. The status read is a single broker ping and never discovers, sends or opens.
"""
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import broker

CONTRACT_VERSION = '0.1.0'
# Newest first; an unsupported request is answered with the newest (MCP lifecycle).
PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
PHASE = 'connectivity_only'
ENABLED_TOOLS = ('bridge_status',)
LIMITS = {'max_message_bytes': 32768, 'max_wait_ms': 5000, 'max_result_page_chars': 16000,
          'operation_retention_hours': 168, 'idempotency_retention_hours': 720}
PING_TIMEOUT = 2
MAX_BODY = 1024 * 1024
TOOLS = json.loads(Path(__file__).resolve().with_name('mcp_tools.json').read_text(encoding='utf-8'))['tools']
if [t['name'] for t in TOOLS] != list(ENABLED_TOOLS):
    raise RuntimeError('mcp_tools.json must list exactly the enabled tools')


def _compute_runtime_revision():
    here = Path(__file__).resolve()
    parts = here.parts
    if 'releases' in parts:
        idx = parts.index('releases')
        if idx + 1 < len(parts) and re.fullmatch(r'[A-Za-z0-9._-]{7,64}', parts[idx + 1]):
            return parts[idx + 1]
    h = hashlib.sha256()
    for name in ('mcp_server.py', 'mcp_phase1.py'):
        candidate = here.with_name(name)
        if candidate.is_file():
            h.update(candidate.read_bytes())
    return 'sha256:' + h.hexdigest()[:16]


def schema_revision(tools):
    raw = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return 'sha256:' + hashlib.sha256(raw).hexdigest()[:16]


RUNTIME_REVISION = _compute_runtime_revision()


def daemon_status(path, timeout=PING_TIMEOUT):
    """ready = ping answered with an instance; unreachable = no connection; degraded = anything else."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return 'unreachable'
    with sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(Path(path).expanduser()))
        except OSError:
            return 'unreachable'
        try:
            sock.sendall(broker.wire({'op': 'ping'}))
            with sock.makefile('rb') as reader:
                line = reader.readline(broker.MAX_FRAME + 1)
            result = json.loads(line)['result']
        except (OSError, ValueError, KeyError, TypeError):
            return 'degraded'
    instance = result.get('instance') if isinstance(result, dict) else None
    return 'ready' if isinstance(instance, str) and instance else 'degraded'


def status(path):
    return {'contract_version': CONTRACT_VERSION,
            'runtime_revision': RUNTIME_REVISION,
            'tool_schema_revision': schema_revision(TOOLS),
            'supported_agent_types': [],
            'server_time': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'phase': PHASE, 'daemon_status': daemon_status(path), 'writes_enabled': False,
            'enabled_tools': list(ENABLED_TOOLS), 'allowed_workspace_aliases': [],
            'capabilities': {'dedicated_claude_sessions': False, 'asynchronous_operations': False,
                             'durable_idempotency': False},
            'limits': dict(LIMITS)}


def tool_result(payload):
    text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    return {'content': [{'type': 'text', 'text': text}], 'structuredContent': payload,
            'isError': not payload['ok']}


def error(mid, code, message):
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}


def handle(message, path, engine=None):
    """One JSON-RPC message in, one response or None (notification/response) out."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return error(None, -32600, 'Invalid Request: one JSON-RPC 2.0 object; batches are not supported')
    if 'method' not in message:
        return None  # a client response; this server sends no requests
    method, mid = message['method'], message.get('id')
    if 'id' not in message:
        return None
    params = message.get('params') or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return error(mid, -32600, 'Invalid Request')
    if method == 'initialize':
        asked = params.get('protocolVersion')
        return {'jsonrpc': '2.0', 'id': mid, 'result': {
            'protocolVersion': asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'agent-bridge', 'version': CONTRACT_VERSION}}}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': engine.tools() if engine else TOOLS}}
    if method == 'tools/call':
        name = params.get('name')
        if name not in ([t['name'] for t in engine.tools()] if engine else ENABLED_TOOLS):
            return error(mid, -32602, f'Unknown tool: {name}')
        arguments = params.get('arguments')
        if engine is not None:
            from mcp_phase1 import AGENT_TYPES, Fault
            if name == 'bridge_status':
                try:
                    engine.authorized(name, engine.principal)
                    engine.validate(name, {} if arguments is None else arguments)
                    data = status(path)
                    enabled_defs = engine.tools()
                    data.update(phase='enabled', writes_enabled=True,
                                tool_schema_revision=schema_revision(enabled_defs),
                                supported_agent_types=list(AGENT_TYPES),
                                enabled_tools=[t['name'] for t in enabled_defs],
                                allowed_workspace_aliases=sorted(engine.config['workspaces']),
                                capabilities=dict.fromkeys(data['capabilities'], True))
                    payload = {'ok': True, 'data': data}
                except Fault as exc:
                    payload = {'ok': False, 'error': exc.payload}
            else:
                payload = engine.call(name, {} if arguments is None else arguments)
            return {'jsonrpc': '2.0', 'id': mid, 'result': tool_result(payload)}
        if arguments not in (None, {}):
            return {'jsonrpc': '2.0', 'id': mid, 'result': tool_result({'ok': False, 'error': {
                'code': 'INVALID_ARGUMENT', 'message': 'bridge_status takes no arguments',
                'retryable': False}})}
        return {'jsonrpc': '2.0', 'id': mid, 'result': tool_result({'ok': True, 'data': status(path)})}
    return error(mid, -32601, f'Method not found: {method}')


def serve_stdio(path, stdin=sys.stdin, stdout=sys.stdout, engine=None):
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = error(None, -32700, 'Parse error')
        else:
            response = handle(message, path, engine)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
            stdout.flush()


def http_handler(path, origins):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10
        server_version = 'agent-bridge-mcp'

        def log_message(self, *_):
            pass  # no request logging: headers may carry tunnel metadata

        def reply(self, code, payload=None, headers=()):
            body = b'' if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            for key, value in headers:
                self.send_header(key, value)
            if payload is not None:
                self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def routed(self):
            if self.path.split('?', 1)[0] != '/mcp':
                self.reply(404)
                return False
            origin = self.headers.get('Origin')
            if origin is not None and origin not in origins:
                self.reply(403, error(None, -32600, 'Origin not allowed'))
                return False
            return True

        def do_GET(self):
            if self.routed():
                self.reply(405, headers=[('Allow', 'POST')])  # no server-sent stream in this MVP

        do_DELETE = do_PUT = do_GET

        def do_POST(self):
            if not self.routed():
                return
            accept = self.headers.get('Accept')
            if accept and not any(t in accept for t in ('application/json', 'text/event-stream', '*/*')):
                return self.reply(406, error(None, -32600, 'Accept must allow application/json'))
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError:
                return self.reply(411, error(None, -32600, 'Content-Length required'))
            if length < 0 or length > MAX_BODY:
                return self.reply(413, error(None, -32600, 'Request too large'))
            try:
                message = json.loads(self.rfile.read(length).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                return self.reply(400, error(None, -32700, 'Parse error'))
            version = self.headers.get('MCP-Protocol-Version')
            initializing = isinstance(message, dict) and message.get('method') == 'initialize'
            if version is not None and not initializing and version not in PROTOCOL_VERSIONS:
                return self.reply(400, error(None, -32600, f'Unsupported MCP-Protocol-Version: {version}'))
            response = handle(message, path)
            if response is None:
                return self.reply(202)
            self.reply(400 if response.get('id') is None and 'error' in response else 200, response)
    return Handler


def serve_http(path, port, extra_origins=()):
    server = ThreadingHTTPServer(('127.0.0.1', port), None)
    bound = server.server_address[1]
    origins = {f'http://127.0.0.1:{bound}', f'http://localhost:{bound}', *extra_origins}
    server.RequestHandlerClass = http_handler(path, origins)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--socket', default=str(broker.DEFAULT_SOCKET))
    parser.add_argument('--http', type=int, metavar='PORT',
                        help='Serve Streamable HTTP POST /mcp on 127.0.0.1:PORT instead of stdio')
    parser.add_argument('--allow-origin', action='append', default=[], metavar='ORIGIN',
                        help='Extra exact Origin accepted over HTTP (loopback origins are always accepted)')
    parser.add_argument('--phase1-config', metavar='FILE',
                        help='Explicit approved private-tunnel principal/workspace config; stdio only')
    parser.add_argument('--recover-registration', metavar='OPERATION_ID',
                        help='Explicit local repair of known created UUID; never creates or sends')
    args = parser.parse_args(argv)
    if args.recover_registration and not args.phase1_config:
        parser.error('--recover-registration requires the approved Phase 1 configuration')
    if args.phase1_config and args.http is not None:
        parser.error('Phase 1 is private-tunnel stdio only; unauthenticated HTTP cannot enable writes')
    if args.http is None:
        engine = None
        if args.phase1_config:
            from mcp_phase1 import Backend, Engine, load_config
            engine = Engine(load_config(args.phase1_config), Backend(args.socket), start=not args.recover_registration)
        try:
            if args.recover_registration:
                print(json.dumps({'ok': True, 'data': engine.recover_registration(args.recover_registration)}))
            else:
                serve_stdio(args.socket, engine=engine)
        finally:
            if engine:
                engine.close()
        return 0
    server = serve_http(args.socket, args.http, args.allow_origin)
    print(json.dumps({'mcp': f'http://127.0.0.1:{server.server_address[1]}/mcp'}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
