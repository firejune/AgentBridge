import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mcp_server
from support import RunningBroker

try:
    import jsonschema
except ImportError:
    jsonschema = None

CALL = {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'bridge_status','arguments':{}}}
OTHER_TOOLS = ('bridge_sessions_list','bridge_models_list','bridge_session_create','bridge_message_send','bridge_operation_get')


class FakeBroker:
    """Unix socket that records each request and answers with a fixed line."""
    def __init__(self, answer):
        self.answer, self.ops = answer, []
        self.temp = tempfile.TemporaryDirectory(prefix='ab-',dir='/tmp')
        self.path = str(Path(self.temp.name)/'b.sock')
        self.server = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.server.bind(self.path);self.server.listen()
        self.thread = threading.Thread(target=self.run,daemon=True);self.thread.start()

    def run(self):
        while True:
            try:
                conn,_ = self.server.accept()
            except OSError:
                return
            with conn, conn.makefile('rb') as reader:
                self.ops.append(json.loads(reader.readline())['op'])
                conn.sendall(self.answer)

    def close(self):
        self.server.close();self.temp.cleanup()


class StatusTest(unittest.TestCase):
    def check(self, payload):
        data = payload['data']
        self.assertEqual(data['phase'],'connectivity_only')
        self.assertIs(data['writes_enabled'],False)
        self.assertEqual(data['enabled_tools'],['bridge_status'])
        self.assertEqual(data['allowed_workspace_aliases'],[])
        self.assertEqual(data['supported_agent_types'],[])
        self.assertTrue(isinstance(data['runtime_revision'],str) and data['runtime_revision'])
        self.assertTrue(isinstance(data['tool_schema_revision'],str) and data['tool_schema_revision'])
        self.assertEqual(set(data['capabilities'].values()),{False})
        self.assertRegex(data['server_time'],r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        if jsonschema:
            jsonschema.validate(payload,mcp_server.TOOLS[0]['outputSchema'])
        return data

    def test_ready_against_a_live_broker(self):
        with RunningBroker() as running:
            result = mcp_server.handle(CALL,running.path)['result']
            self.assertIs(result['isError'],False)
            self.assertEqual(json.loads(result['content'][0]['text']),result['structuredContent'])
            self.assertEqual(self.check(result['structuredContent'])['daemon_status'],'ready')

    def test_unreachable_is_a_valid_observation(self):
        with tempfile.TemporaryDirectory(prefix='ab-',dir='/tmp') as temp:
            result = mcp_server.handle(CALL,str(Path(temp)/'missing.sock'))['result']
        self.assertIs(result['isError'],False)
        self.assertEqual(self.check(result['structuredContent'])['daemon_status'],'unreachable')

    def test_degraded_when_connected_but_malformed_and_only_pings(self):
        for answer in (b'not json\n', b'{"error":"boom"}\n', b'{"result":{"instance":""}}\n', b''):
            fake = FakeBroker(answer)
            try:
                data = self.check(mcp_server.handle(CALL,fake.path)['result']['structuredContent'])
                self.assertEqual(data['daemon_status'],'degraded',answer)
                self.assertEqual(fake.ops,['ping'])
            finally:
                fake.close()

    def test_arguments_are_refused_as_a_tool_error(self):
        call = json.loads(json.dumps(CALL));call['params']['arguments'] = {'cwd':'/'}
        result = mcp_server.handle(call,'/nonexistent')['result']
        self.assertIs(result['isError'],True)
        self.assertEqual(result['structuredContent']['error']['code'],'INVALID_ARGUMENT')
        if jsonschema:
            jsonschema.validate(result['structuredContent'],mcp_server.TOOLS[0]['outputSchema'])


@unittest.skipIf(jsonschema is None, 'optional jsonschema unavailable')
class ModelToolSchemaTest(unittest.TestCase):
    def setUp(self):
        scripts = Path(__file__).resolve().parents[1] / 'skill' / 'agent-bridge' / 'scripts'
        tools = json.loads((scripts / 'mcp_phase1_tools.json').read_text())['tools']
        self.tools = {tool['name']: tool for tool in tools}

    def test_model_catalog_schema_accepts_public_fields_only(self):
        tool = self.tools['bridge_models_list']
        arguments = {'workspace_alias': 'sample', 'agent_type': 'codex'}
        jsonschema.validate(arguments, tool['inputSchema'])
        payload = {'ok': True, 'data': {
            **arguments, 'supports_model_selection': True,
            'default_model': None, 'models': [{'id': 'sample-model', 'label': None}],
        }}
        jsonschema.validate(payload, tool['outputSchema'])
        payload['data']['models'][0]['provider_metadata'] = 'private'
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(payload, tool['outputSchema'])
        payload['data']['models'] = [{'id': 'sample-model', 'label': None}] * 201
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(payload, tool['outputSchema'])

    def test_create_model_schema_preserves_omission_and_bounds_explicit_id(self):
        tool = self.tools['bridge_session_create']
        arguments = {'workspace_alias': 'sample', 'idempotency_key': 'example-key-1234'}
        for model in ({}, {'model': 'x' * 128}):
            jsonschema.validate({**arguments, **model}, tool['inputSchema'])
        for model in ('', ' \t\n', 'x' * 129, None, 42):
            with self.subTest(model=model), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({**arguments, 'model': model}, tool['inputSchema'])
        error = {'ok': False, 'error': {
            'code': 'UNSUPPORTED_MODEL_SELECTION', 'message': 'Unsupported.', 'retryable': False,
        }}
        jsonschema.validate(error, tool['outputSchema'])


class ProtocolTest(unittest.TestCase):
    def test_initialize_negotiates(self):
        for asked,expected in (('2025-06-18','2025-06-18'),('2024-11-05','2025-11-25'),(None,'2025-11-25')):
            result = mcp_server.handle({'jsonrpc':'2.0','id':1,'method':'initialize',
                                        'params':{'protocolVersion':asked}},'/x')['result']
            self.assertEqual(result['protocolVersion'],expected)
            self.assertEqual(result['capabilities'],{'tools':{'listChanged':False}})

    def test_phase_0_lists_and_runs_only_status(self):
        tools = mcp_server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'},'/x')['result']['tools']
        self.assertEqual([t['name'] for t in tools],['bridge_status'])
        self.assertEqual(tools[0]['annotations']['readOnlyHint'],True)
        for name in OTHER_TOOLS+('shell',):
            response = mcp_server.handle({'jsonrpc':'2.0','id':3,'method':'tools/call',
                                          'params':{'name':name,'arguments':{}}},'/x')
            self.assertEqual(response['error']['code'],-32602,name)

    def test_notifications_ping_and_errors(self):
        self.assertIsNone(mcp_server.handle({'jsonrpc':'2.0','method':'notifications/initialized'},'/x'))
        self.assertIsNone(mcp_server.handle({'jsonrpc':'2.0','id':9,'result':{}},'/x'))
        self.assertEqual(mcp_server.handle({'jsonrpc':'2.0','id':4,'method':'ping'},'/x')['result'],{})
        self.assertEqual(mcp_server.handle({'jsonrpc':'2.0','id':5,'method':'resources/list'},'/x')['error']['code'],-32601)
        self.assertEqual(mcp_server.handle([CALL],'/x')['error']['code'],-32600)

    def test_stdio_round_trip(self):
        lines = [{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},
                 {'jsonrpc':'2.0','method':'notifications/initialized'}, CALL]
        stdin = io.StringIO('\n'.join(json.dumps(m) for m in lines)+'\nnot json\n')
        stdout = io.StringIO()
        mcp_server.serve_stdio('/nonexistent',stdin,stdout)
        out = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([m.get('id') for m in out],[1,1,None])
        self.assertEqual(out[1]['result']['structuredContent']['data']['daemon_status'],'unreachable')
        self.assertEqual(out[2]['error']['code'],-32700)

    def test_stdio_process(self):
        with RunningBroker() as running:
            process = subprocess.run([sys.executable,str(Path(mcp_server.__file__)),'--socket',running.path],
                                     input=json.dumps(CALL)+'\n',capture_output=True,text=True,timeout=10)
        self.assertEqual(process.returncode,0,process.stderr)
        result = json.loads(process.stdout)['result']['structuredContent']
        self.assertEqual(result['data']['daemon_status'],'ready')


class ModernProtocolTest(unittest.TestCase):
    def setUp(self):
        self.engine = SimpleNamespace(config={'mcp_2026_enabled': True}, events=None,
                                      tools=lambda: mcp_server.TOOLS)

    def request(self, method, mid=1, **params):
        return {'jsonrpc': '2.0', 'id': mid, 'method': method, 'params': {
            '_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                      'io.modelcontextprotocol/clientCapabilities': {}}, **params}}

    def test_discovery_and_tools_need_no_initialize_and_legacy_stays_legacy(self):
        result = mcp_server.handle(self.request('server/discover'), '/missing', self.engine)['result']
        self.assertEqual(result['supportedVersions'], ['2026-07-28', *mcp_server.PROTOCOL_VERSIONS])
        self.assertEqual(result['capabilities'], {'tools': {'listChanged': False}})
        self.assertEqual(result['_meta']['io.modelcontextprotocol/serverInfo']['name'], 'agent-bridge')
        self.assertEqual((result['ttlMs'], result['cacheScope']), (0, 'private'))
        for method in ('ping', 'tools/list'):
            modern = mcp_server.handle(self.request(method), '/missing', self.engine)['result']
            self.assertEqual(modern['resultType'], 'complete')
            if method == 'tools/list':
                self.assertEqual((modern['ttlMs'], modern['cacheScope']), (0, 'private'))
            else:
                self.assertNotIn('ttlMs', modern)
                self.assertNotIn('cacheScope', modern)
        legacy_tools = mcp_server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, '/missing', self.engine)['result']
        self.assertEqual(set(legacy_tools), {'tools'})
        legacy = {'jsonrpc': '2.0', 'id': 2, 'method': 'initialize', 'params': {'protocolVersion': '2026-07-28'}}
        result = mcp_server.handle(legacy, '/missing', self.engine)['result']
        self.assertEqual(result['protocolVersion'], '2025-11-25')
        self.assertNotIn('resultType', result)
        self.assertEqual(mcp_server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'}, '/missing', self.engine)['result'], {})
        self.assertEqual(mcp_server.handle(self.request('initialize'), '/missing', self.engine)['error']['code'], -32601)
        self.engine.config['mcp_2026_enabled'] = False
        self.assertEqual(mcp_server.handle(self.request('server/discover'), '/missing', self.engine)['error']['code'], -32601)

    def test_metadata_errors_prevent_tools_and_no_capabilities_are_inherited(self):
        for value in (None, [], 'private-token'):
            message = self.request('tools/call', name='bridge_status')
            message['params']['_meta']['io.modelcontextprotocol/clientCapabilities'] = value
            with patch.object(mcp_server, '_handle') as handler:
                self.assertEqual(mcp_server.handle(message, '/missing', self.engine)['error']['code'], -32602)
                handler.assert_not_called()
        for params in ({}, {'_meta': None}, {'_meta': {}}, {'_meta': {'io.modelcontextprotocol/protocolVersion': False}}):
            message = self.request('server/discover'); message['params'] = params
            self.assertEqual(mcp_server.handle(message, '/missing', self.engine)['error']['code'], -32602)
        message = self.request('tools/list')
        message['params']['_meta']['io.modelcontextprotocol/protocolVersion'] = '2027-01-01'
        response = mcp_server.handle(message, '/missing', self.engine)['error']
        self.assertEqual(response['code'], -32022)
        self.assertEqual(response['data']['requested'], '2027-01-01')
        self.assertIn('2026-07-28', response['data']['supported'])
        for key, value in [('io.modelcontextprotocol/clientInfo', None), ('progressToken', True)]:
            message = self.request('tools/list'); message['params']['_meta'][key] = value
            self.assertEqual(mcp_server.handle(message, '/missing', self.engine)['error']['code'], -32602)

    def test_cancellation_suppresses_response_preserves_running_effect_and_skips_queued_effect(self):
        entered, release = threading.Event(), threading.Event()
        effects = []
        original = mcp_server._handle
        def blocking(message, path, engine, modern=False):
            if message.get('method') == 'tools/call':
                effects.append(message['id']); entered.set()
                self.assertTrue(release.wait(3))
                return {'jsonrpc': '2.0', 'id': message['id'], 'result': {'content': []}}
            return original(message, path, engine, modern=modern)
        class Reader:
            def __init__(reader): reader.index = 0
            def readline(reader, size):
                reader.index += 1
                if reader.index == 1: return json.dumps(self.request('tools/call', 'active')) + '\n'
                if reader.index == 2:
                    self.assertTrue(entered.wait(3))
                    return json.dumps(self.request('tools/call', 'queued')) + '\n'
                if reader.index in (3, 4):
                    return json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                        'params': {'requestId': 'active' if reader.index == 3 else 'queued'}}) + '\n'
                if reader.index == 5:
                    release.set()
                    return json.dumps(self.request('ping', 'after')) + '\n'
                return ''
        output = io.StringIO()
        with patch.object(mcp_server, '_handle', blocking):
            mcp_server.serve_stdio('/missing', Reader(), output, self.engine)
        self.assertEqual(effects, ['active'])
        self.assertEqual([row['id'] for row in map(json.loads, output.getvalue().splitlines())], ['after'])
        self.assertFalse(self.engine._mcp_stdio_worker.is_alive())

    def test_capacity_and_shutdown_are_bounded(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocking(message, path, engine, modern=False):
            calls.append(message['id']); entered.set(); release.wait(3)
            return {'jsonrpc': '2.0', 'id': message['id'], 'result': {}}
        class Reader:
            def __init__(reader): reader.index = 0
            def readline(reader, size):
                reader.index += 1
                if reader.index == 2: self.assertTrue(entered.wait(3))
                if reader.index <= 3: return json.dumps(self.request('ping', reader.index)) + '\n'
                release.set(); return ''
        output = io.StringIO()
        with patch.object(mcp_server, '_handle', blocking), patch.object(mcp_server, 'STDIO_CAPACITY', 2):
            mcp_server.serve_stdio('/missing', Reader(), output, self.engine)
        rows = list(map(json.loads, output.getvalue().splitlines()))
        self.assertEqual(next(row for row in rows if row['id'] == 3)['error']['code'], -32603)
        self.assertEqual(calls, [1, 2])
        release.clear()
        try:
            with patch.object(mcp_server, '_handle', blocking), patch.object(mcp_server, 'STDIO_SHUTDOWN_SECONDS', 0.01):
                with self.assertRaisesRegex(RuntimeError, 'shutdown'):
                    mcp_server.serve_stdio('/missing', io.StringIO(json.dumps(self.request('ping')) + '\n'), io.StringIO(), self.engine)
            self.assertTrue(self.engine._mcp_stdio_worker.is_alive())
        finally:
            release.set(); self.engine._mcp_stdio_worker.join(3)

    def test_cancellation_is_effective_before_slow_receipt_audit(self):
        entered, release, response_finished, written = (threading.Event() for _ in range(4))
        def blocking(message, path, engine, modern=False):
            if message.get('method') == 'tools/call':
                entered.set(); self.assertTrue(release.wait(3))
                return {'jsonrpc': '2.0', 'id': 1, 'result': {'content': []}}
            return None
        class Audit:
            def audit_request(audit, message):
                if message.get('method') == 'notifications/cancelled':
                    release.set()
                    self.assertTrue(response_finished.wait(3))
                    self.assertFalse(written.wait(0.1), 'cancelled response escaped during receipt audit')
            def audit_response(audit, message, response):
                if message.get('method') == 'tools/call': response_finished.set()
        class Output(io.StringIO):
            def write(output, value): written.set(); return super().write(value)
        class Reader:
            def __init__(reader): reader.index = 0
            def readline(reader, size):
                reader.index += 1
                if reader.index == 1: return json.dumps(self.request('tools/call'))+'\n'
                if reader.index == 2:
                    self.assertTrue(entered.wait(3))
                    return json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 1}})+'\n'
                return ''
        self.engine.events = Audit()
        output = Output()
        try:
            with patch.object(mcp_server, '_handle', blocking):
                mcp_server.serve_stdio('/missing', Reader(), output, self.engine)
        finally:
            release.set(); self.engine._mcp_stdio_worker.join(3)
        self.assertEqual(output.getvalue(), '')

    def test_oversized_utf8_frame_is_drained_without_losing_next_request(self):
        valid = {'jsonrpc': '2.0', 'id': 9, 'method': 'ping'}
        output = io.StringIO()
        with patch.object(mcp_server, 'MAX_BODY', 128):
            mcp_server.serve_stdio('/missing', io.StringIO('é'*100+'\n'+json.dumps(valid)+'\n'), output, self.engine)
        rows = list(map(json.loads, output.getvalue().splitlines()))
        self.assertEqual(rows[0]['error']['code'], -32600)
        self.assertEqual(rows[1], {'jsonrpc': '2.0', 'id': 9, 'result': {}})

    def test_actual_stdio_process_discovers_calls_tools_and_retains_legacy_fallback(self):
        with tempfile.TemporaryDirectory(prefix='ab-modern-', dir='/tmp') as temp:
            root = Path(temp).resolve()
            config = {'principal': 'test-owner', 'tunnel_id': 'test-tunnel', 'writes_enabled': True,
                'mcp_2026_enabled': True, 'state_dir': str(root/'state'),
                'scopes': ['bridge:read', 'bridge:session:create', 'bridge:message:send'],
                'workspaces': {'test': {'cwd': str(root), 'root': str(root), 'project': 'test', 'ide': 'antigravity-ide'}}}
            path = root/'config.json'; path.write_text(json.dumps(config)); path.chmod(0o600)
            messages = [self.request('server/discover', 1), self.request('tools/list', 2),
                self.request('tools/call', 3, name='bridge_status', arguments={}),
                {'jsonrpc': '2.0', 'id': 4, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
                {'jsonrpc': '2.0', 'id': 5, 'method': 'ping'}]
            process = subprocess.run([sys.executable, str(Path(mcp_server.__file__)), '--socket', str(root/'absent.sock'),
                '--phase1-config', str(path)], input=''.join(json.dumps(message)+'\n' for message in messages),
                capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        rows = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([row['id'] for row in rows], [1, 2, 3, 4, 5])
        self.assertEqual([row['result'].get('resultType') for row in rows], ['complete']*3 + [None, None])
        self.assertEqual(len(rows[1]['result']['tools']), 6)
        for row in rows[:2]:
            self.assertEqual((row['result']['ttlMs'], row['result']['cacheScope']), (0, 'private'))
        for row in rows[2:]:
            self.assertNotIn('ttlMs', row['result'])
            self.assertNotIn('cacheScope', row['result'])
        self.assertTrue(rows[2]['result']['structuredContent']['ok'])
        self.assertEqual(rows[3]['result']['protocolVersion'], '2025-11-25')


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.server = mcp_server.serve_http('/nonexistent',0,['https://allowed.example'])
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/mcp'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close()

    def send(self, method='POST', body=None, path='/mcp', **headers):
        data = None if body is None else (body if isinstance(body,bytes) else json.dumps(body).encode())
        request = urllib.request.Request(self.url.replace('/mcp',path),data=data,method=method,
                                         headers={'Content-Type':'application/json',
                                                  'Accept':'application/json, text/event-stream',**headers})
        try:
            with urllib.request.urlopen(request,timeout=5) as response:
                return response.status,response.read()
        except urllib.error.HTTPError as error:
            return error.code,error.read()

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')

    def test_call_returns_200_json(self):
        code,body = self.send(body=CALL,**{'MCP-Protocol-Version':'2025-11-25'})
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)['result']['structuredContent']['data']['daemon_status'],'unreachable')

    def test_origin_is_checked(self):
        self.assertEqual(self.send(body=CALL,Origin='https://evil.example')[0],403)
        self.assertEqual(self.send(body=CALL,Origin='https://allowed.example')[0],200)
        self.assertEqual(self.send(body=CALL,Origin=self.url.rsplit('/',1)[0])[0],200)

    def test_transport_rules(self):
        self.assertEqual(self.send(body={'jsonrpc':'2.0','method':'notifications/initialized'}),(202,b''))
        self.assertEqual(self.send('GET')[0],405)
        self.assertEqual(self.send(body=CALL,path='/other')[0],404)
        self.assertEqual(self.send(body=CALL,**{'MCP-Protocol-Version':'1999-01-01'})[0],400)
        self.assertEqual(self.send(body=b'{oops')[0],400)
        self.assertEqual(self.send(body=[CALL])[0],400)
        self.assertEqual(self.send(body=CALL,Accept='text/html')[0],406)


if __name__ == '__main__':
    unittest.main()
