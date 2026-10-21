"""Opt-in per-user launchd supervision; generation is safe without loading."""
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL = 'com.agent-bridge.broker'


def plist_path(value, label):
    return Path(value).expanduser().absolute() if value else Path.home() / 'Library/LaunchAgents' / (label + '.plist')


def configuration(socket, state_dir, max_messages):
    socket = Path(socket).expanduser().absolute()
    state_dir = Path(state_dir).expanduser().absolute() if state_dir else socket.with_suffix('.state')
    if max_messages < 1:
        raise ValueError('Message retention size must be positive')
    label = LABEL + '.' + hashlib.sha256(str(socket).encode()).hexdigest()[:16]
    return dict(Label=label,
                ProgramArguments=[sys.executable, str(Path(__file__).with_name('bridge.py').resolve()),
                                  '--socket', str(socket), 'serve', '--state-dir', str(state_dir),
                                  '--max-messages', str(max_messages)],
                RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
                StandardOutPath=str(state_dir / 'service.stdout.log'),
                StandardErrorPath=str(state_dir / 'service.stderr.log'),
                Umask=0o077)


def launchctl(*args):
    process = subprocess.run(['launchctl', *args], capture_output=True, text=True, timeout=15)
    if process.returncode:
        # Do not print arbitrary launchd output or environment metadata.
        raise ValueError('launchctl failed; inspect the generated plist and per-user service state')


def install(socket, state_dir=None, plist=None, generate_only=False, max_messages=2048):
    if sys.platform != 'darwin' and not generate_only:
        raise ValueError('Installing a launchd service requires macOS')
    config = configuration(socket, state_dir, max_messages)
    if generate_only and not plist:
        raise ValueError('--generate-only requires an explicit --plist review path')
    path = plist_path(plist, config['Label'])
    if generate_only and path.parent.resolve() == (Path.home() / 'Library/LaunchAgents').resolve():
        raise ValueError('Generate review plists outside LaunchAgents to avoid login activation')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite another service (or repeat an ambiguous installation).
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as output:
        plistlib.dump(config, output)
    if generate_only:
        return dict(plist=str(path), installed=False, generated=True)
    directory = Path(config['StandardOutPath']).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError('Service state directory must be owner-private (0700); plist retained')
    launchctl('bootstrap', f'gui/{os.getuid()}', str(path))
    return dict(plist=str(path), installed=True, label=config['Label'])


def uninstall(socket, state_dir=None, plist=None, generate_only=False):
    if sys.platform != 'darwin' and not generate_only:
        raise ValueError('Uninstalling a launchd service requires macOS')
    expected = configuration(socket, state_dir, 2048)
    path = plist_path(plist, expected['Label'])
    with path.open('rb') as source:
        config = plistlib.load(source)
    # Match the executable, broker, socket and state directory, allowing a
    # different retention size. Refuse to remove an unrelated agent.
    if (config.get('Label') != expected['Label']
            or config.get('ProgramArguments', [])[:-1] != expected['ProgramArguments'][:-1]):
        raise ValueError('Plist does not match this broker service')
    if not generate_only:
        launchctl('bootout', f'gui/{os.getuid()}/{expected["Label"]}')
    path.unlink()
    return dict(plist=str(path), installed=False, removed=True)
