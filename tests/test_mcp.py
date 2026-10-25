import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
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
OTHER_TOOLS = ('bridge_sessions_list','bridge_session_create','bridge_message_send','bridge_operation_get')


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
