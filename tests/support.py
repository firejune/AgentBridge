import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import broker


class RunningBroker:
    def __init__(self, max_messages=2048, usage_interval=0, codex_executable=None):
        # Limit sampling is off unless a test names a stub app-server.
        self.max_messages = max_messages
        self.usage = ['--usage-interval', str(usage_interval)]
        if codex_executable:
            self.usage += ['--codex-executable', codex_executable]

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ab-',dir='/tmp')
        self.path = str(Path(self.temp.name)/'b.sock')
        self.state_dir = str(Path(self.temp.name)/'state')
        self.start()
        return self

    def start(self):
        cli = str(Path(broker.__file__).with_name('bridge.py'))
        self.process = subprocess.Popen([sys.executable,cli,'--socket',self.path,'serve',
            '--state-dir',self.state_dir,'--max-messages',str(self.max_messages),*self.usage],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        ready = selectors.DefaultSelector()
        try:
            ready.register(self.process.stdout,selectors.EVENT_READ)
            if not ready.select(10):
                raise RuntimeError('Broker startup timed out')
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(self.process.stderr.read())
            self.instance = json.loads(line)['instance']
        except BaseException:
            self.process.kill();self.process.communicate();self.temp.cleanup()
            raise
        finally:
            ready.close()

    def restart(self):
        self.call('shutdown')
        self.process.communicate(timeout=2)
        self.start()

    def __exit__(self,*_):
        if self.process.poll() is None:
            try:
                self.call('shutdown')
            except (ValueError,OSError):
                self.process.terminate()
        try:
            self.output,self.errors = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill();self.output,self.errors=self.process.communicate()
        self.temp.cleanup()

    def call(self,op,**args):
        return broker.request(self.path,{'op':op,**args})

    def register_pair(self):
        for name in ('alpha','beta'):
            self.call('register',project='demo',name=name,cwd=self.temp.name,transport='realtime')
            self.call('channel-bind',project='demo',name=name,owner=name,description=name)
