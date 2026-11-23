#!/usr/bin/env python3
"""Move a supervised Agent Bridge install to one commit. See docs/DEPLOY.md.

Three things run from release directories named by a commit id: the CLI and
skill (three symlink pins), the broker (a launchd plist that names its
release), and the MCP facade (one command line in the tunnel profile). This
moves the chosen ones to the same commit, in the order earlier deploys used:
stop facade, stop broker, start broker, start facade.

Nothing is stopped before the state is copied aside, every refusal has a name,
and nothing is retried: a step that fails ends the run with the rollback
command printed.
"""
import argparse
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/agent-bridge/scripts'))
import service  # The broker's launchd label is derived in one place.

COMPONENTS = ('cli', 'broker', 'facade')
SHA = r'[0-9a-f]{40}'
SKILL = 'skill/agent-bridge'
BROKER_STATE_FILES = ('routes.json', 'correspondence.json', 'correspondence-index.json', 'policy.json')
FACADE_STATE_FILES = ('events.json', 'ledger.sqlite3')


class Refusal(Exception):
    """A precondition that failed; nothing was changed."""

    def __init__(self, code, detail):
        super().__init__(f'{code}: {detail}')
        self.code = code


class StepFailed(Exception):
    """A step failed after changes began; the receipt and rollback command say where."""


@dataclass
class Layout:
    repo: Path
    runtime: Path
    socket: Path
    launch_agents: Path
    tunnel_profile: Path
    pins: tuple

    @property
    def state(self):
        return self.socket.with_suffix('.state')

    @property
    def broker_label(self):
        return service.configuration(self.socket, None, 1)['Label']

    @property
    def broker_plist(self):
        return self.launch_agents / (self.broker_label + '.plist')

    def release(self, kind, sha):
        """kind: 'broker' for the broker runtime; 'nana' for the CLI and facade runtime."""
        return self.runtime / f'{kind}-runtime' / 'releases' / sha

    @classmethod
    def default(cls, repo):
        home = Path.home()
        runtime = home / '.local/share/agent-bridge'
        return cls(repo=repo, runtime=runtime, socket=runtime / 'bridge.sock',
                   launch_agents=home / 'Library/LaunchAgents',
                   tunnel_profile=home / '.config/agent-bridge/nana/tunnel.yaml',
                   pins=tuple(home / base / 'skills/agent-bridge' for base in ('.agents', '.codex', '.claude')))


class System:
    """Every effect outside the runtime directory tree. Tests replace this."""

    def git(self, repo, *args):
        done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout.strip()

    def archive(self, repo, sha, destination):
        done = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', sha],
                              capture_output=True, timeout=120, check=True)
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
            tar.extractall(destination, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))

    def launchctl(self, *args):
        return subprocess.run(['launchctl', *args], capture_output=True, text=True, timeout=30).returncode

    def loaded(self, label):
        return self.launchctl('print', f'gui/{self.uid()}/{label}') == 0

    def pid(self, label):
        done = subprocess.run(['launchctl', 'list', label], capture_output=True, text=True, timeout=10)
        found = re.search(r'"PID" = (\d+);', done.stdout)
        return int(found[1]) if done.returncode == 0 and found else None

    def cli(self, bridge, socket, *args):
        """Run one read-only bridge command with the given release's own CLI."""
        done = subprocess.run([sys.executable, str(bridge), '--socket', str(socket), *args],
                              capture_output=True, text=True, timeout=30)
        try:
            return done.returncode, json.loads(done.stdout.splitlines()[0])
        except (ValueError, IndexError):
            return done.returncode or 1, None

    def ready(self, url_file):
        try:
            with urllib.request.urlopen(url_file.read_text().strip() + '/readyz', timeout=3) as reply:
                return reply.status == 200
        except (OSError, ValueError):
            return False

    def uid(self):
        return os.getuid()

    def sleep(self, seconds):
        time.sleep(seconds)


@dataclass
class Live:
    """What is installed now, read without changing anything."""
    pins: dict
    cli: str
    broker: str
    facade: str
    facade_label: str
    facade_plist: Path
    ping: dict = field(default_factory=dict)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def only_sha(pattern, text, code, what):
    found = set(re.findall(pattern, text, re.M))
    if len(found) != 1:
        raise Refusal(code, f'{what} names {len(found)} releases; expected exactly one')
    return found.pop()


def read_pins(layout):
    pins = {}
    for pin in layout.pins:
        if not pin.is_symlink():
            raise Refusal('PIN_NOT_A_LINK', f'{pin} is not a symbolic link to a release')
        pins[str(pin)] = os.readlink(pin)
    return pins


def read_facade(layout):
    """The release the tunnel profile names, and the launchd agent that runs the profile."""
    prefix = re.escape(str(layout.runtime))
    if not layout.tunnel_profile.is_file():
        raise Refusal('FACADE_PROFILE_MISSING', 'the tunnel profile is missing')
    # The profile also holds a credential: read it, never print or copy it.
    facade = only_sha(rf'{prefix}/nana-runtime/releases/({SHA})/', layout.tunnel_profile.read_text(encoding='utf-8'),
                      'FACADE_PROFILE_UNREADABLE', 'the tunnel profile')
    facade_label = facade_plist = None
    for candidate in sorted(layout.launch_agents.glob('*.plist')):
        try:
            with candidate.open('rb') as source:
                data = plistlib.load(source)
        except Exception:  # Another program's plist, unreadable or not a plist: not ours to judge.
            continue
        if str(layout.tunnel_profile) in map(str, data.get('ProgramArguments') or []):
            facade_label, facade_plist = data.get('Label'), candidate
    if facade_label is None:
        raise Refusal('FACADE_SERVICE_MISSING', 'no launchd plist runs this tunnel profile')
    return facade, facade_label, facade_plist


def point_facade(layout, current, target):
    """Rewrite the release id in the tunnel profile in place; the rest of the file is kept as it is."""
    prefix = f'{layout.runtime}/nana-runtime/releases/'
    text = layout.tunnel_profile.read_text(encoding='utf-8')
    atomic_bytes(layout.tunnel_profile, text.replace(prefix + current + '/', prefix + target + '/').encode('utf-8'))


def read_live(layout, components):
    pins = read_pins(layout)
    prefix = re.escape(str(layout.runtime))
    targets = set(pins.values())
    pinned = re.fullmatch(rf'{prefix}/nana-runtime/releases/({SHA})/{SKILL}', targets.pop()) if len(targets) == 1 else None
    if pinned is None:
        raise Refusal('PINS_DISAGREE', 'the skill pins do not all point at one release of this runtime')
    cli = pinned[1]
    if not layout.broker_plist.is_file():
        raise Refusal('BROKER_SERVICE_MISSING', f'no launchd plist {layout.broker_plist.name}')
    with layout.broker_plist.open('rb') as source:
        arguments = plistlib.load(source).get('ProgramArguments') or []
    broker = only_sha(rf'^{prefix}/broker-runtime/releases/({SHA})/{SKILL}/scripts/bridge\.py$',
                      '\n'.join(map(str, arguments)), 'BROKER_SERVICE_UNREADABLE', 'the broker plist')
    facade = facade_label = facade_plist = None
    if 'facade' in components:
        facade, facade_label, facade_plist = read_facade(layout)
    return Live(pins, cli, broker, facade, facade_label, facade_plist)


def correspondence_states(layout):
    counts = {}
    for message in read_json(layout.state / 'correspondence.json')['messages']:
        counts[message['state']] = counts.get(message['state'], 0) + 1
    return counts


# A close handoff whose operator request may still move. A reservation with no request (manual
# handoff) or a failed or undelivered one is stranded: it survives a restart and waits for
# `close --finish`, so it does not hold a deploy (#196).
ACTIVE_CLOSE_STATES = ('dispatching', 'submitted', 'in_progress', 'uncertain')


def route_counts(layout):
    routes = read_json(layout.state / 'routes.json')
    requests = {message['close_request'].get('close_id'): message['state']
                for message in read_json(layout.state / 'correspondence.json')['messages']
                if isinstance(message.get('close_request'), dict)}
    closing = [item['closing'] for item in routes['endpoints'] if 'closing' in item]
    active = sum(requests.get(close_id) in ACTIVE_CLOSE_STATES for close_id in closing)
    return dict(endpoints=len(routes['endpoints']), channels=len(routes['channels']),
                closing=active, stranded_closing=len(closing) - active)


def route_identity(endpoint):
    return tuple(endpoint.get(key) for key in ('project', 'name', 'transport', 'session'))


def saved_endpoints(layout):
    """Each endpoint's identity and saved workspace, read before the restart."""
    return {route_identity(item): item.get('cwd') for item in read_json(layout.state / 'routes.json')['endpoints']}


def route_changes(layout, before):
    """Endpoints gone after the restart, split by whether the broker was right to drop them.

    The broker does not restore a route whose workspace is unavailable (it logs `routing_drop`
    with that reason). Such a drop is the route's own state, not damage from the deploy.
    """
    after = saved_endpoints(layout)
    missing = [before[key] for key in before if key not in after]
    expected = sum(1 for cwd in missing if isinstance(cwd, str) and not Path(cwd).is_dir())
    return dict(dropped_workspace_missing=expected, dropped_other=len(missing) - expected,
                added=sum(1 for key in after if key not in before))


def check_source(layout, system, sha, live, components, allow_non_ancestor):
    code, dirty = system.git(layout.repo, 'status', '--porcelain')
    if code or dirty:
        raise Refusal('SOURCE_DIRTY', 'the checkout this script runs from has uncommitted changes')
    if system.git(layout.repo, 'merge-base', '--is-ancestor', sha, 'origin/main')[0]:
        raise Refusal('SOURCE_NOT_ON_MAIN', f'{sha[:7]} is not contained in origin/main')
    if all(getattr(live, name) == sha for name in components):
        raise Refusal('TARGET_ALREADY_LIVE', f'{", ".join(components)} already run {sha[:7]}')
    for name in components:
        current = getattr(live, name)
        if current != sha and system.git(layout.repo, 'merge-base', '--is-ancestor', current, sha)[0]:
            if not allow_non_ancestor:
                _, lost = system.git(layout.repo, 'rev-list', '--count', f'{sha}..{current}')
                raise Refusal('LIVE_NOT_IN_TARGET',
                              f'the live {name} release {current[:7]} carries {lost or "unknown"} commit(s) that '
                              f'{sha[:7]} does not; pass --allow-non-ancestor to drop them')


def check_quiet(layout, system, live, components):
    bridge = Path(next(iter(live.pins))) / 'scripts/bridge.py'  # The installed CLI, through its pin.
    code, ping = system.cli(bridge, layout.socket, 'ping')
    if code or not isinstance(ping, dict):
        raise Refusal('BROKER_UNREACHABLE', 'the running broker did not answer ping')
    live.ping = ping
    if ping.get('correspondence_lost'):
        raise Refusal('CORRESPONDENCE_LOST', 'the broker reports lost correspondence; repair that first')
    if not {'broker', 'facade'} & set(components):
        return None
    states, routes = correspondence_states(layout), route_counts(layout)
    if states.get('dispatching'):
        raise Refusal('DELIVERIES_IN_FLIGHT', f'{states["dispatching"]} message(s) are dispatching')
    if routes['closing']:
        raise Refusal('CLOSE_HANDOFF_OPEN', f'{routes["closing"]} route(s) are mid close handoff')
    stranded = routes['stranded_closing']
    code, plan = system.cli(bridge, layout.socket, 'retention', '--live')
    waits = plan.get('active_waits') if isinstance(plan, dict) else None
    if code or waits is None:
        raise Refusal('BROKER_UNREACHABLE', 'the running broker did not report its active waits')
    if waits:
        raise Refusal('WAITS_ACTIVE', f'{waits} caller(s) are waiting on a reply through this broker')
    return dict(dispatching=0, closing=0, waits=0, stranded_closing=stranded)


def plan_steps(layout, live, sha, components, uid):
    """The steps in order, as (name, description). The real run performs exactly these."""
    steps = []
    if {'cli', 'facade'} & set(components):
        steps.append(('build cli release', f'git archive {sha} -> {layout.release("nana", sha)}'))
    if 'broker' in components:
        steps.append(('build broker release', f'git archive {sha} -> {layout.release("broker", sha)}'))
    steps.append(('snapshot', f'copy state, plists, pins and the facade release id to {layout.runtime}/deploy-{sha[:7]}-*'))
    if 'facade' in components:
        steps.append(('stop facade', f'launchctl bootout gui/{uid}/{live.facade_label}'))
    if 'broker' in components:
        steps.append(('stop broker', f'launchctl bootout gui/{uid}/{layout.broker_label}'))
        steps.append(('point broker', f'{layout.broker_plist.name}: {live.broker[:7]} -> {sha[:7]}'))
        steps.append(('start broker', f'launchctl bootstrap gui/{uid} {layout.broker_plist}'))
    if 'cli' in components:
        steps.append(('move pins', f'{len(layout.pins)} pins: {live.cli[:7]} -> {sha[:7]}'))
    if 'facade' in components:
        steps.append(('point facade', f'{layout.tunnel_profile.name}: {live.facade[:7]} -> {sha[:7]}'))
        steps.append(('start facade', f'launchctl bootstrap gui/{uid} {live.facade_plist}'))
    steps.append(('verify', 'ping with the new CLI: builds equal, no skew warning, routes and correspondence kept'))
    return steps


def generated(path, root):
    """Bytecode Python writes beside the sources once a release has run; not part of the commit."""
    return '__pycache__' in path.relative_to(root).parts or path.suffix == '.pyc'


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob('*') if (p.is_file() or p.is_symlink()) and not generated(p, root)):
        digest.update(str(path.relative_to(root)).encode() + b'\0')
        digest.update(os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
    return digest.hexdigest()


def build_release(layout, system, kind, sha):
    """Extract the commit beside its final place, then rename; an existing release must be identical."""
    final = layout.release(kind, sha)
    final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f'.{sha[:7]}-', dir=final.parent))
    try:
        system.archive(layout.repo, sha, staging)
        if not (staging / SKILL / 'scripts/bridge.py').is_file():
            raise StepFailed(f'the archive of {sha[:7]} holds no {SKILL}/scripts/bridge.py')
        if final.exists():
            if tree_digest(final) != tree_digest(staging):
                raise Refusal('RELEASE_MISMATCH', f'{final} exists and differs from commit {sha[:7]}')
            return final
        staging.rename(final)
        return final
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def atomic_bytes(path, data, mode=0o600):
    handle, name = tempfile.mkstemp(prefix='.deploy-', dir=path.parent)
    with os.fdopen(handle, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(name, mode)
    os.replace(name, path)


def move_pin(pin, target):
    staged = pin.with_name(f'.{pin.name}.deploy')
    if staged.is_symlink():
        staged.unlink()
    os.symlink(target, staged)
    os.replace(staged, pin)


def snapshot(layout, live, sha, components):
    folder = Path(tempfile.mkdtemp(prefix=f'deploy-{sha[:7]}-', dir=layout.runtime))
    os.chmod(folder, 0o700)
    copies = [(layout.broker_plist, layout.broker_plist.name)]
    copies += [(layout.state / name, name) for name in BROKER_STATE_FILES]
    if 'facade' in components:
        # Not the tunnel profile: it holds a credential. Rollback needs only the release id it named.
        copies += [(live.facade_plist, live.facade_plist.name)]
        copies += [(layout.runtime / 'nana-runtime/state' / name, name) for name in FACADE_STATE_FILES]
    for source, name in copies:
        if source.is_file():
            shutil.copy2(source, folder / name)
            os.chmod(folder / name, 0o600)
    atomic_bytes(folder / 'cli-pins.json', json.dumps(live.pins).encode())
    if 'facade' in components:
        atomic_bytes(folder / 'facade.json', json.dumps(dict(release=live.facade)).encode())
    return folder


def stop(system, label, actions, name):
    started = time.monotonic()
    bootout_exit = system.launchctl('bootout', f'gui/{system.uid()}/{label}')
    for _ in range(150):  # Graceful shutdown writes the checkpoint first.
        if not system.loaded(label):
            code = 0
            break
        system.sleep(.2)
    else:
        code = bootout_exit or 1
    actions[name] = dict(seconds=round(time.monotonic() - started, 3), exit=code)
    if bootout_exit != code:
        actions[name]['bootout_exit'] = bootout_exit
    if code:
        raise StepFailed(f'{name}: launchctl bootout did not stop {label}')


def start(system, label, plist, actions, name):
    started = time.monotonic()
    code = system.launchctl('bootstrap', f'gui/{system.uid()}', str(plist))
    actions[name] = dict(seconds=round(time.monotonic() - started, 3), exit=code)
    if code:
        raise StepFailed(f'{name}: launchctl bootstrap refused {plist.name}')


def wait_ping(layout, system, bridge):
    # The broker listens only after it has verified every saved route, which took
    # over two minutes with 100 routes on a loaded machine (#238). Wait up to ten.
    for _ in range(2000):
        code, ping = system.cli(bridge, layout.socket, 'ping')
        if code == 0 and isinstance(ping, dict):
            return ping
        system.sleep(.3)
    raise StepFailed('the broker did not answer ping after its start')


def apply(layout, system, live, sha, components, folder, result):
    actions = result['actions'] = {}
    prefix = str(layout.runtime)
    if 'facade' in components:
        stop(system, live.facade_label, actions, 'stop facade')
    if 'broker' in components:
        stop(system, layout.broker_label, actions, 'stop broker')
        with layout.broker_plist.open('rb') as source:
            config = plistlib.load(source)
        config['ProgramArguments'] = [str(value).replace(f'{prefix}/broker-runtime/releases/{live.broker}/',
                                                         f'{prefix}/broker-runtime/releases/{sha}/')
                                      for value in config['ProgramArguments']]
        atomic_bytes(layout.broker_plist, plistlib.dumps(config))
        start(system, layout.broker_label, layout.broker_plist, actions, 'start broker')
        wait_ping(layout, system, layout.release('broker', sha) / SKILL / 'scripts/bridge.py')
    if 'cli' in components:
        for pin in layout.pins:
            move_pin(pin, str(layout.release('nana', sha) / SKILL))
    if 'facade' in components:
        point_facade(layout, live.facade, sha)
        start(system, live.facade_label, live.facade_plist, actions, 'start facade')


def verify(layout, system, live, sha, components, before, result):
    after = read_live(layout, components)
    bridge = Path(next(iter(after.pins))) / 'scripts/bridge.py'
    ping = wait_ping(layout, system, bridge)
    states, routes = correspondence_states(layout), route_counts(layout)
    changes = route_changes(layout, before['endpoints'])
    result.update(
        cli_sha=after.cli, broker_sha=after.broker, facade_sha=after.facade,
        broker_pid=system.pid(layout.broker_label), previous_broker_pid=before['broker_pid'],
        tunnel_pid=system.pid(after.facade_label) if after.facade_label else None,
        build=ping.get('build'), cli_build=ping.get('cli_build'), build_warning=ping.get('warning'),
        correspondence=states, correspondence_lost=ping.get('correspondence_lost'),
        endpoint_count=routes['endpoints'], channel_count=routes['channels'],
        subscribers=ping.get('subscribers'), previous_subscribers=live.ping.get('subscribers'),
        routes_dropped_workspace_missing=changes['dropped_workspace_missing'],
        routes_preserved=(changes['dropped_other'] == 0 and changes['added'] == 0
                          and routes['channels'] == before['routes']['channels']),
        protected_correspondence_preserved=all(states.get(state, 0) >= count for state, count in
                                               before['states'].items() if state in ('pending', 'uncertain')),
        pins=len(after.pins))
    if 'facade' in components:
        url = layout.runtime / 'nana-runtime/health.url'
        for _ in range(40):
            result['facade_ready'] = system.ready(url)
            if result['facade_ready']:
                break
            system.sleep(.5)
    problems = [name for name, bad in (
        ('pins or services do not name the target', any(getattr(after, name) != sha for name in components)),
        # Also after a partial move: leaving two builds running is the failure this script exists to prevent.
        ('CLI and broker builds differ',
         ping.get('build') is None or ping.get('build') != ping.get('cli_build') or bool(ping.get('warning'))),
        ('correspondence_lost is set', bool(ping.get('correspondence_lost'))),
        ('routes changed', not result['routes_preserved']),
        ('pending or uncertain correspondence went missing', not result['protected_correspondence_preserved']),
        ('the facade is not ready', 'facade' in components and not result.get('facade_ready')),
    ) if bad]
    if problems:
        raise StepFailed('; '.join(problems))


def write_receipt(folder, result):
    atomic_bytes(folder / 'result.json', (json.dumps(result, indent=2) + '\n').encode())


def recover_facade(layout, system, live, sha, result):
    actions = result['actions']
    if actions.get('stop facade', {}).get('exit') != 0:
        return
    recovery = result['facade_recovery'] = dict(release=live.facade)
    try:
        if 'start facade' in actions:
            if system.loaded(live.facade_label):
                stop(system, live.facade_label, actions, 'stop facade for recovery')
            point_facade(layout, sha, live.facade)
        start(system, live.facade_label, live.facade_plist, actions, 'restart previous facade')
    except (StepFailed, OSError, subprocess.SubprocessError) as error:
        recovery.update(restarted=False, error=str(error))
    else:
        recovery['restarted'] = True


def rollback_command(folder):
    return f'python3 {Path(__file__).resolve()} --rollback {folder}'


def deploy(layout, system, commit, components, dry_run=False, allow_non_ancestor=False, out=print):
    code, sha = system.git(layout.repo, 'rev-parse', '--verify', commit + '^{commit}')
    if code or not re.fullmatch(SHA, sha):
        raise Refusal('SOURCE_UNKNOWN', f'{commit!r} does not name a commit in this checkout')
    live = read_live(layout, components)
    out(f'live:   cli {live.cli[:7]}  broker {live.broker[:7]}  facade {live.facade[:7] if live.facade else "-"}')
    out(f'target: {sha} for {", ".join(components)}')
    check_source(layout, system, sha, live, components, allow_non_ancestor)
    in_flight = check_quiet(layout, system, live, components)
    steps = plan_steps(layout, live, sha, components, system.uid())
    for number, (name, detail) in enumerate(steps, 1):
        out(f'{number:>2}. {name}: {detail}')
    if dry_run:
        out('dry run: nothing was changed')
        return 0
    started = time.monotonic()
    before = dict(broker_pid=system.pid(layout.broker_label), routes=route_counts(layout),
                  endpoints=saved_endpoints(layout), states=correspondence_states(layout))
    if {'cli', 'facade'} & set(components):
        release = build_release(layout, system, 'nana', sha)
        if system.cli(release / SKILL / 'scripts/bridge.py', layout.socket, 'ping')[0]:
            raise Refusal('RELEASE_UNUSABLE', 'the new CLI release could not ping the running broker')
    if 'broker' in components:
        build_release(layout, system, 'broker', sha)
    folder = snapshot(layout, live, sha, components)
    out(f'snapshot: {folder}')
    out(f'rollback: {rollback_command(folder)}')
    result = dict(sha=sha, components=list(components), previous=dict(cli=live.cli, broker=live.broker,
                                                                    facade=live.facade), in_flight=in_flight)
    try:
        apply(layout, system, live, sha, components, folder, result)
        verify(layout, system, live, sha, components, before, result)
    except StepFailed as error:
        recover_facade(layout, system, live, sha, result)
        result.update(error=str(error), elapsed_seconds=round(time.monotonic() - started, 3))
        write_receipt(folder, result)
        out(f'FAILED: {error}')
        out(f'receipt: {folder / "result.json"}')
        out(f'rollback: {rollback_command(folder)}')
        return 1
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    write_receipt(folder, result)
    out(f'deployed {sha[:7]}: build {result["build"]}, {result["endpoint_count"]} routes, '
        f'broker pid {result["broker_pid"]}')
    if result['routes_dropped_workspace_missing']:
        out(f"warning: the broker dropped {result['routes_dropped_workspace_missing']} route(s) whose workspace "
            'no longer exists')
    out(f'receipt: {folder / "result.json"}')
    return 0


def rollback(layout, system, folder, dry_run=False, out=print):
    """Put back the pins, the broker plist and the facade release id a deploy saved. State files stay as they are.

    The current pins may disagree (an interrupted deploy leaves them so); the services are identified
    from the broker plist and the tunnel profile, not from the pins.
    """
    folder = Path(folder)
    saved_plist = folder / layout.broker_plist.name
    pins = read_json(folder / 'cli-pins.json') if (folder / 'cli-pins.json').is_file() else {}
    if set(pins) != {str(pin) for pin in layout.pins} or not saved_plist.is_file():
        raise Refusal('SNAPSHOT_UNUSABLE', f'{folder} does not hold this install\'s pins and broker plist')
    saved_facade = read_json(folder / 'facade.json').get('release') if (folder / 'facade.json').is_file() else None
    if saved_facade is not None and not re.fullmatch(SHA, str(saved_facade)):
        raise Refusal('SNAPSHOT_UNUSABLE', f'{folder}/facade.json names no release')
    read_pins(layout)  # Each pin must still be a link; its target does not matter here.
    facade = read_facade(layout) if saved_facade else None
    uid = system.uid()
    plist_changed = not layout.broker_plist.is_file() or layout.broker_plist.read_bytes() != saved_plist.read_bytes()
    broker_loaded = system.loaded(layout.broker_label)
    steps = []
    if facade is not None and system.loaded(facade[1]):
        steps.append(('stop facade', f'launchctl bootout gui/{uid}/{facade[1]}'))
    if plist_changed:
        if broker_loaded:
            steps.append(('stop broker', f'launchctl bootout gui/{uid}/{layout.broker_label}'))
        steps.append(('restore broker plist', f'{layout.broker_plist.name} from {folder.name}'))
    if plist_changed or not broker_loaded:
        steps.append(('start broker', f'launchctl bootstrap gui/{uid} {layout.broker_plist}'))
    steps.append(('restore pins', f'{len(pins)} pins from cli-pins.json'))
    if facade is not None:
        steps.append(('point facade', f'{layout.tunnel_profile.name}: {facade[0][:7]} -> {saved_facade[:7]}'))
        steps.append(('start facade', f'launchctl bootstrap gui/{uid} {facade[2]}'))
    steps.append(('verify', 'ping with the restored CLI'))
    for number, (name, detail) in enumerate(steps, 1):
        out(f'{number:>2}. {name}: {detail}')
    if dry_run:
        out('dry run: nothing was changed')
        return 0
    actions = {}
    names = {name for name, _ in steps}
    if 'stop facade' in names:
        stop(system, facade[1], actions, 'stop facade')
    if 'stop broker' in names:
        stop(system, layout.broker_label, actions, 'stop broker')
    if 'restore broker plist' in names:
        atomic_bytes(layout.broker_plist, saved_plist.read_bytes())
    if 'start broker' in names:
        start(system, layout.broker_label, layout.broker_plist, actions, 'start broker')
    for pin, target in pins.items():
        move_pin(Path(pin), target)
    if facade is not None:
        if facade[0] != saved_facade:
            point_facade(layout, facade[0], saved_facade)
        start(system, facade[1], facade[2], actions, 'start facade')
    ping = wait_ping(layout, system, Path(next(iter(pins.values()))) / 'scripts/bridge.py')
    atomic_bytes(folder / 'rollback.json', json.dumps(dict(actions=actions, build=ping.get('build')), indent=2).encode())
    out(f'rolled back from {folder}: broker answers, build {ping.get("build")}')
    return 0


def main(argv=None, system=None, layout=None):
    parser = argparse.ArgumentParser(description='Move the supervised Agent Bridge install to one commit.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--commit', help='Commit on origin/main to deploy')
    mode.add_argument('--rollback', metavar='SNAPSHOT_DIR', help='Restore the pins and services a deploy saved')
    parser.add_argument('--components', default=','.join(COMPONENTS),
                        help='Comma-separated subset of cli,broker,facade (default: all, so one build runs everywhere)')
    parser.add_argument('--dry-run', action='store_true', help='Print every step and change nothing (also with --rollback)')
    parser.add_argument('--allow-non-ancestor', action='store_true',
                        help='Deploy although a live release carries commits the target lacks')
    args = parser.parse_args(argv)
    system = system or System()
    layout = layout or Layout.default(Path(__file__).resolve().parents[1])
    try:
        if args.rollback:
            return rollback(layout, system, args.rollback, args.dry_run)
        components = tuple(name for name in COMPONENTS if name in args.components.split(','))
        if not components or set(args.components.split(',')) - set(COMPONENTS):
            raise Refusal('COMPONENTS_UNKNOWN', f'choose from {", ".join(COMPONENTS)}')
        return deploy(layout, system, args.commit, components, args.dry_run, args.allow_non_ancestor)
    except Refusal as refusal:
        print(f'REFUSED {refusal}', flush=True)
        return 2
    except StepFailed as error:
        print(f'FAILED: {error}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
