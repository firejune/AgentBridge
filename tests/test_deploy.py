"""tools/deploy.py against a fake runtime directory: no launchd, no git, no live state."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import deploy

OLD, NEW, PATCHED = 'a' * 40, 'b' * 40, 'c' * 40
FACADE_LABEL = 'example.facade'


class FakeSystem:
    """A machine with one broker and one facade service, both loaded and answering."""

    def __init__(self, layout):
        self.layout = layout
        self.calls, self.loaded_labels = [], {layout.broker_label, FACADE_LABEL}
        self.pids = {layout.broker_label: 100, FACADE_LABEL: 200}
        self.dirty, self.waits, self.answers, self.ready_after = '', 0, True, True
        self.on_main = {NEW, OLD}
        self.ancestors = {(OLD, NEW)}  # (older, newer)
        self.skewed_cli = False

    def git(self, repo, *args):
        if args[0] == 'rev-parse':
            name = args[-1].removesuffix('^{commit}')
            return (0, name) if re.fullmatch('[0-9a-f]{40}', name) else (1, '')
        if args[0] == 'status':
            return 0, self.dirty
        if args[0] == 'merge-base':
            older, newer = args[2], args[3]
            if newer == 'origin/main':
                return (0 if older in self.on_main else 1), ''
            return (0 if older == newer or (older, newer) in self.ancestors else 1), ''
        if args[0] == 'rev-list':
            return 0, '3'
        raise AssertionError(args)

    def archive(self, repo, sha, destination):
        scripts = Path(destination) / deploy.SKILL / 'scripts'
        scripts.mkdir(parents=True)
        (scripts / 'bridge.py').write_text(f'# {sha}\n')

    def launchctl(self, *args):
        self.calls.append(args)
        label = args[1].rsplit('/', 1)[-1] if args[0] == 'bootout' else plistlib.loads(Path(args[2]).read_bytes())['Label']
        if args[0] == 'bootout':
            if label not in self.loaded_labels:
                return 3
            self.loaded_labels.remove(label)
        else:
            self.loaded_labels.add(label)
            self.pids[label] += 1
        return 0

    def loaded(self, label):
        return label in self.loaded_labels

    def pid(self, label):
        return self.pids[label] if label in self.loaded_labels else None

    def release_of(self, text):
        return re.search('[0-9a-f]{40}', str(text))[0]

    def cli(self, bridge, socket, *args):
        if not self.answers or self.layout.broker_label not in self.loaded_labels:
            return 1, None
        if args[0] == 'retention':
            return 0, {'active_waits': self.waits}
        running = self.release_of(plistlib.loads(self.layout.broker_plist.read_bytes())['ProgramArguments'][1])
        caller = self.release_of(os.path.realpath(bridge))
        reply = {'build': running[:12], 'cli_build': caller[:12], 'subscribers': 4, 'correspondence_lost': False}
        if running != caller or self.skewed_cli:
            reply['warning'] = 'BROKER_BUILD_SKEW: …'
        return 0, reply

    def ready(self, url_file):
        return self.ready_after

    def uid(self):
        return 501

    def sleep(self, seconds):
        pass


class DeployFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        runtime = root / 'runtime'
        self.layout = deploy.Layout(repo=root / 'repo', runtime=runtime, socket=runtime / 'bridge.sock',
                                    launch_agents=root / 'LaunchAgents', tunnel_profile=root / 'config/tunnel.yaml',
                                    pins=tuple(root / name / 'skills/agent-bridge' for name in ('one', 'two', 'three')))
        for kind in ('nana', 'broker'):
            scripts = self.layout.release(kind, OLD) / deploy.SKILL / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'bridge.py').write_text(f'# {OLD}\n')
        for pin in self.layout.pins:
            pin.parent.mkdir(parents=True)
            os.symlink(self.layout.release('nana', OLD) / deploy.SKILL, pin)
        self.layout.launch_agents.mkdir()
        self.broker_arguments = ['/usr/bin/python3',
                                 str(self.layout.release('broker', OLD) / deploy.SKILL / 'scripts/bridge.py'),
                                 '--socket', str(self.layout.socket), 'serve', '--max-messages', '2048', '--retention-ttl']
        self.layout.broker_plist.write_bytes(plistlib.dumps(dict(
            Label=self.layout.broker_label, ProgramArguments=self.broker_arguments, KeepAlive=True, Umask=63)))
        self.facade_plist = self.layout.launch_agents / (FACADE_LABEL + '.plist')
        self.facade_plist.write_bytes(plistlib.dumps(dict(
            Label=FACADE_LABEL, ProgramArguments=['/opt/tunnel', 'run', '--profile-file', str(self.layout.tunnel_profile)])))
        self.layout.tunnel_profile.parent.mkdir()
        self.layout.tunnel_profile.write_text(
            'api_key: FIXTURE-SECRET\nmcp:\n  commands:\n    - command: "python3 '
            f'{self.layout.release("nana", OLD)}/skill/agent-bridge/scripts/mcp_server.py --phase1-config x"\n')
        self.layout.state.mkdir()
        self.write_state([('pending', 3), ('submitted', 2), ('uncertain', 1)], closing=0)
        (self.layout.state / 'policy.json').write_text('{}')
        (runtime / 'nana-runtime/state').mkdir()
        (runtime / 'nana-runtime/state/events.json').write_text('{}')
        self.system = FakeSystem(self.layout)
        self.lines = []

    def write_state(self, states, closing, close_state='submitted'):
        messages = [{'id': f'{state}-{n}', 'state': state} for state, count in states for n in range(count)]
        if closing:  # The operator request of the handoff, unless close_state is None (manual handoff).
            messages += [] if close_state is None else [
                {'id': 'close', 'state': close_state, 'close_request': {'close_id': 'x'}}]
        (self.layout.state / 'correspondence.json').write_text(json.dumps({'version': 1, 'messages': messages}))
        endpoints = [{'name': f'r{n}', **({'closing': 'x'} if n < closing else {})} for n in range(5)]
        (self.layout.state / 'routes.json').write_text(json.dumps(
            {'version': 1, 'endpoints': endpoints, 'channels': [{'name': 'c'}]}))

    def run_deploy(self, commit=NEW, components=deploy.COMPONENTS, **options):
        return deploy.deploy(self.layout, self.system, commit, components, out=self.lines.append, **options)

    def fingerprint(self):
        """Every file and link under the fixture, so 'changed nothing' is a measurement."""
        root = self.layout.runtime.parent
        digest = hashlib.sha256()
        for path in sorted(root.rglob('*')):
            digest.update(str(path.relative_to(root)).encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def refusal(self, **options):
        before = self.fingerprint()
        with self.assertRaises(deploy.Refusal) as raised:
            self.run_deploy(**options)
        self.assertEqual(self.fingerprint(), before, 'a refusal changed the runtime')
        self.assertEqual(self.system.calls, [], 'a refusal reached launchctl')
        return raised.exception.code


class DryRunAndRefusalTests(DeployFixture):
    def test_dry_run_prints_every_step_in_order_and_changes_nothing(self):
        before = self.fingerprint()
        self.assertEqual(self.run_deploy(dry_run=True), 0)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.system.calls, [])
        self.assertEqual(self.lines[0], f'live:   cli {OLD[:7]}  broker {OLD[:7]}  facade {OLD[:7]}')
        self.assertEqual(self.lines[1], f'target: {NEW} for cli, broker, facade')
        steps = [re.match(r' *\d+\. ([a-z ]+):', line)[1] for line in self.lines[2:-1]]
        self.assertEqual(steps, ['build cli release', 'build broker release', 'snapshot', 'stop facade', 'stop broker',
                                 'point broker', 'start broker', 'move pins', 'point facade', 'start facade', 'verify'])
        self.assertEqual(self.lines[-1], 'dry run: nothing was changed')
        self.assertNotIn('FIXTURE-SECRET', '\n'.join(self.lines))

    def test_an_unreadable_plist_of_another_program_is_passed_over(self):
        (self.layout.launch_agents / 'another.program.plist').write_text('<?xml version="1.0"?><plist><&></plist>')
        self.assertEqual(self.run_deploy(dry_run=True), 0)

    def test_cli_only_plan_touches_no_service(self):
        self.assertEqual(self.run_deploy(components=('cli',), dry_run=True), 0)
        self.assertNotIn('launchctl', '\n'.join(self.lines))

    def test_each_precondition_is_refused_by_name_before_anything_changes(self):
        def case(code, arrange, **options):
            self.setUp()
            arrange()
            with self.subTest(code=code):
                self.assertEqual(self.refusal(**options), code)

        case('SOURCE_UNKNOWN', lambda: None, commit='not-a-commit')
        case('SOURCE_DIRTY', lambda: setattr(self.system, 'dirty', ' M tools/deploy.py'))
        case('SOURCE_NOT_ON_MAIN', lambda: self.system.on_main.discard(NEW))
        case('TARGET_ALREADY_LIVE', lambda: None, commit=OLD)
        case('BROKER_UNREACHABLE', lambda: setattr(self.system, 'answers', False))
        case('DELIVERIES_IN_FLIGHT', lambda: self.write_state([('dispatching', 1), ('pending', 1)], closing=0))
        case('CLOSE_HANDOFF_OPEN', lambda: self.write_state([('pending', 1)], closing=2))
        case('WAITS_ACTIVE', lambda: setattr(self.system, 'waits', 2))
        case('PIN_NOT_A_LINK', lambda: (self.layout.pins[1].unlink(), self.layout.pins[1].mkdir()))
        case('PINS_DISAGREE', lambda: deploy.move_pin(self.layout.pins[2], str(self.layout.release('nana', NEW) / deploy.SKILL)))
        case('BROKER_SERVICE_MISSING', lambda: self.layout.broker_plist.unlink())
        case('FACADE_PROFILE_UNREADABLE', lambda: self.layout.tunnel_profile.write_text('api_key: FIXTURE-SECRET\n'))
        case('FACADE_SERVICE_MISSING', lambda: self.facade_plist.unlink())

    def test_a_live_release_with_commits_the_target_lacks_needs_the_explicit_flag(self):
        self.layout.release('broker', OLD).rename(self.layout.release('broker', PATCHED))
        self.broker_arguments[1] = self.broker_arguments[1].replace(OLD, PATCHED)
        self.layout.broker_plist.write_bytes(plistlib.dumps(dict(
            Label=self.layout.broker_label, ProgramArguments=self.broker_arguments)))
        with self.assertRaisesRegex(deploy.Refusal, r'LIVE_NOT_IN_TARGET: the live broker release ccccccc carries 3 commit'):
            self.run_deploy(dry_run=True)
        self.assertEqual(self.run_deploy(dry_run=True, allow_non_ancestor=True), 0)

    def test_a_stranded_close_handoff_does_not_hold_a_broker_deploy(self):
        for close_state in ('failed', 'pending', None):
            with self.subTest(close_state=close_state):
                self.write_state([('pending', 1)], closing=1, close_state=close_state)
                self.assertEqual(deploy.check_quiet(self.layout, self.system, deploy.read_live(self.layout, deploy.COMPONENTS),
                                                    deploy.COMPONENTS)['stranded_closing'], 1)
        for close_state in deploy.ACTIVE_CLOSE_STATES:
            with self.subTest(close_state=close_state):
                self.write_state([('pending', 1)], closing=1, close_state=close_state)
                # A dispatching request is also a delivery in flight, refused first under that name.
                with self.assertRaisesRegex(deploy.Refusal, 'CLOSE_HANDOFF_OPEN|DELIVERIES_IN_FLIGHT'):
                    deploy.check_quiet(self.layout, self.system, deploy.read_live(self.layout, deploy.COMPONENTS),
                                       deploy.COMPONENTS)

    def test_a_quiet_check_is_not_needed_to_move_only_the_cli(self):
        self.write_state([('dispatching', 1)], closing=1)
        self.system.waits = 5
        self.assertEqual(self.run_deploy(components=('cli',), dry_run=True), 0)

    def test_command_line_reports_refusals_with_exit_2(self):
        self.system.dirty = '?? stray'
        self.assertEqual(deploy.main(['--commit', NEW, '--dry-run'], system=self.system, layout=self.layout), 2)
        self.assertEqual(deploy.main(['--commit', NEW, '--components', 'cli,db'], system=self.system, layout=self.layout), 2)


class RealRunTests(DeployFixture):
    def test_full_deploy_moves_everything_in_the_recorded_order_and_writes_a_receipt(self):
        self.assertEqual(self.run_deploy(), 0, self.lines)
        uid, broker = 'gui/501', self.layout.broker_label
        self.assertEqual(self.system.calls, [
            ('bootout', f'{uid}/{FACADE_LABEL}'), ('bootout', f'{uid}/{broker}'),
            ('bootstrap', uid, str(self.layout.broker_plist)), ('bootstrap', uid, str(self.facade_plist))])
        target = str(self.layout.release('nana', NEW) / deploy.SKILL)
        self.assertEqual([os.readlink(pin) for pin in self.layout.pins], [target] * 3)
        config = plistlib.loads(self.layout.broker_plist.read_bytes())
        self.assertEqual(config['ProgramArguments'], [value.replace(OLD, NEW) for value in self.broker_arguments])
        self.assertEqual((config['KeepAlive'], config['Umask']), (True, 63))
        profile = self.layout.tunnel_profile.read_text()
        self.assertIn(f'{self.layout.release("nana", NEW)}/skill', profile)
        self.assertNotIn(OLD, profile)
        self.assertIn('api_key: FIXTURE-SECRET', profile)
        folder, = self.layout.runtime.glob(f'deploy-{NEW[:7]}-*')
        self.assertLessEqual({'cli-pins.json', 'routes.json', 'correspondence.json', 'policy.json', 'facade.json',
                              'events.json', self.layout.broker_plist.name, self.facade_plist.name, 'result.json'},
                             {path.name for path in folder.iterdir()})
        self.assertEqual(plistlib.loads((folder / self.layout.broker_plist.name).read_bytes())['ProgramArguments'],
                         self.broker_arguments)
        result = json.loads((folder / 'result.json').read_text())
        self.assertEqual((result['sha'], result['cli_sha'], result['broker_sha'], result['facade_sha']), (NEW,) * 4)
        self.assertEqual(result['previous'], {'cli': OLD, 'broker': OLD, 'facade': OLD})
        self.assertEqual((result['build'], result['cli_build'], result['build_warning']), (NEW[:12], NEW[:12], None))
        self.assertEqual((result['broker_pid'], result['previous_broker_pid'], result['tunnel_pid']), (101, 100, 201))
        self.assertEqual((result['routes_preserved'], result['protected_correspondence_preserved'],
                          result['correspondence_lost'], result['facade_ready'], result['pins']),
                         (True, True, False, True, 3))
        self.assertEqual((result['endpoint_count'], result['channel_count'], result['correspondence']),
                         (5, 1, {'pending': 3, 'submitted': 2, 'uncertain': 1}))
        self.assertEqual(list(result['actions']), ['stop facade', 'stop broker', 'start broker', 'start facade'])
        self.assertNotIn('error', result)
        rollback = next(line for line in self.lines if line.startswith('rollback: '))
        self.assertTrue(rollback.endswith(f'--rollback {folder}'))
        self.assertLess(self.lines.index(rollback), len(self.lines) - 2, 'rollback is printed before the stop')
        self.assertNotIn('FIXTURE-SECRET', '\n'.join(self.lines))

    def test_rollback_restores_pins_services_and_profile_from_the_snapshot(self):
        before = {path: path.read_bytes() for path in (self.layout.broker_plist, self.layout.tunnel_profile)}
        self.run_deploy()
        folder, = self.layout.runtime.glob(f'deploy-{NEW[:7]}-*')
        self.system.calls.clear()
        self.assertEqual(deploy.rollback(self.layout, self.system, folder, out=self.lines.append), 0)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual([os.readlink(pin) for pin in self.layout.pins],
                         [str(self.layout.release('nana', OLD) / deploy.SKILL)] * 3)
        self.assertEqual([call[0] for call in self.system.calls], ['bootout', 'bootout', 'bootstrap', 'bootstrap'])
        self.assertTrue(self.layout.release('nana', NEW).is_dir(), 'a rollback keeps the new release on disk')
        with self.assertRaisesRegex(deploy.Refusal, 'SNAPSHOT_UNUSABLE'):
            deploy.rollback(self.layout, self.system, self.layout.state, out=self.lines.append)

    def test_the_snapshot_holds_no_copy_of_the_credential(self):
        self.assertEqual(self.run_deploy(), 0, self.lines)
        folder, = self.layout.runtime.glob(f'deploy-{NEW[:7]}-*')
        self.assertNotIn('tunnel.yaml', {path.name for path in folder.iterdir()})
        for path in folder.iterdir():
            self.assertNotIn(b'FIXTURE-SECRET', path.read_bytes(), path.name)
        self.assertEqual(json.loads((folder / 'facade.json').read_text()), {'release': OLD})

    def test_rollback_dry_run_prints_the_steps_and_changes_nothing(self):
        self.run_deploy()
        folder, = self.layout.runtime.glob(f'deploy-{NEW[:7]}-*')
        self.system.calls.clear()
        before = self.fingerprint()
        self.assertEqual(deploy.main(['--rollback', str(folder), '--dry-run'], system=self.system, layout=self.layout), 0)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.system.calls, [])
        self.assertEqual(deploy.rollback(self.layout, self.system, folder, dry_run=True, out=self.lines.append), 0)
        tail = self.lines[self.lines.index('dry run: nothing was changed') - 8:]
        steps = [re.match(r' *\d+\. ([a-z ]+):', line)[1] for line in tail[:-1]]
        self.assertEqual(steps, ['stop facade', 'stop broker', 'restore broker plist', 'start broker', 'restore pins',
                                 'point facade', 'start facade', 'verify'])
        self.assertNotIn('FIXTURE-SECRET', '\n'.join(self.lines))

    def test_rollback_restores_pins_that_an_interrupted_deploy_left_mixed(self):
        self.run_deploy()
        folder, = self.layout.runtime.glob(f'deploy-{NEW[:7]}-*')
        deploy.move_pin(self.layout.pins[0], str(self.layout.release('nana', OLD) / deploy.SKILL))
        self.assertEqual(deploy.rollback(self.layout, self.system, folder, out=self.lines.append), 0)
        self.assertEqual([os.readlink(pin) for pin in self.layout.pins],
                         [str(self.layout.release('nana', OLD) / deploy.SKILL)] * 3)
        self.assertIn(OLD, self.layout.tunnel_profile.read_text())

    def test_an_existing_release_that_has_run_is_still_the_same_release(self):
        scripts = self.layout.release('nana', NEW) / deploy.SKILL / 'scripts'
        scripts.mkdir(parents=True)
        (scripts / 'bridge.py').write_text(f'# {NEW}\n')
        (scripts / '__pycache__').mkdir()
        (scripts / '__pycache__/bridge.cpython-312.pyc').write_bytes(b'\x00compiled')
        (scripts / 'stray.pyc').write_bytes(b'\x00compiled')
        self.assertEqual(self.run_deploy(), 0, self.lines)

    def test_cli_only_deploy_leaves_both_services_alone(self):
        self.assertEqual(self.run_deploy(components=('cli',)), 1, 'old broker and new CLI are different builds')
        self.assertEqual(self.system.calls, [])
        self.assertEqual({os.readlink(pin) for pin in self.layout.pins}, {str(self.layout.release('nana', NEW) / deploy.SKILL)})
        self.assertIn(OLD, self.layout.broker_plist.read_bytes().decode(errors='replace'))

    def test_a_failed_check_after_the_switch_recovers_only_the_facade(self):
        cases = (('skewed_cli', True, 'CLI and broker builds differ'), ('ready_after', False, 'the facade is not ready'))
        for attribute, value, problem in cases:
            self.setUp()
            setattr(self.system, attribute, value)
            with self.subTest(problem=problem):
                self.assertEqual(self.run_deploy(), 1)
                folder, = self.layout.runtime.glob(f'deploy-{NEW[:7]}-*')
                result = json.loads((folder / 'result.json').read_text())
                self.assertIn(problem, result['error'])
                self.assertEqual(result['facade_recovery'], dict(release=OLD, restarted=True))
                self.assertEqual(self.system.calls[-2:], [
                    ('bootout', f'gui/501/{FACADE_LABEL}'), ('bootstrap', 'gui/501', str(self.facade_plist))])
                self.assertEqual(len(self.system.calls), 6)
                self.assertTrue(all(NEW in os.readlink(pin) for pin in self.layout.pins))
                self.assertIn(NEW, self.layout.broker_plist.read_text())
                self.assertIn(OLD, self.layout.tunnel_profile.read_text())
                self.assertEqual(self.lines[-1], f'rollback: {deploy.rollback_command(folder)}')

    def test_a_route_dropped_for_a_missing_workspace_warns_and_any_other_route_change_fails(self):
        kept = self.layout.state  # A folder that exists.
        gone = self.layout.runtime / 'removed-worktree'

        def restart_drops(name, cwd):
            self.setUp()
            routes = self.layout.state / 'routes.json'
            saved = {'version': 1, 'channels': [{'name': 'c'}], 'endpoints': [
                {'project': 'p', 'name': 'stays', 'transport': 'codex', 'session': 's1', 'cwd': str(kept)},
                {'project': 'p', 'name': name, 'transport': 'agy', 'session': 's2', 'cwd': str(cwd)}]}
            routes.write_text(json.dumps(saved))
            launchctl = self.system.launchctl

            def dropping(*args):
                if args[0] == 'bootstrap':  # The broker rewrites its routing snapshot when it starts.
                    routes.write_text(json.dumps({**saved, 'endpoints': saved['endpoints'][:1]}))
                return launchctl(*args)

            self.system.launchctl = dropping
            code = self.run_deploy()
            folder, = self.layout.runtime.glob(f'deploy-{NEW[:7]}-*')
            return code, json.loads((folder / 'result.json').read_text())

        code, result = restart_drops('runner', gone)
        self.assertEqual((code, result['routes_preserved'], result['routes_dropped_workspace_missing']), (0, True, 1))
        self.assertIn('warning: the broker dropped 1 route(s) whose workspace no longer exists', self.lines)
        self.assertFalse(any('runner' in line or str(gone) in line for line in self.lines))

        code, result = restart_drops('runner', kept)
        self.assertEqual((code, result['routes_preserved'], result['routes_dropped_workspace_missing']), (1, False, 0))
        self.assertIn('routes changed', result['error'])

    def test_a_broker_that_does_not_come_back_restarts_the_previous_facade(self):
        real = self.system.launchctl

        def launchctl(*args):
            code = real(*args)
            if args == ('bootstrap', 'gui/501', str(self.layout.broker_plist)):
                self.system.answers = False
            return code

        self.system.launchctl = launchctl
        self.assertEqual(self.run_deploy(), 1)
        self.assertIn('FAILED: the broker did not answer ping after its start', self.lines)
        self.assertEqual({os.readlink(pin) for pin in self.layout.pins}, {str(self.layout.release('nana', OLD) / deploy.SKILL)})
        self.assertIn(OLD, self.layout.tunnel_profile.read_text())
        self.assertEqual([call[0] for call in self.system.calls], ['bootout', 'bootout', 'bootstrap', 'bootstrap'])
        self.assertEqual(self.system.calls[-1], ('bootstrap', 'gui/501', str(self.facade_plist)))
        self.assertTrue(self.system.loaded(FACADE_LABEL))
        folder, = self.layout.runtime.glob('deploy-*')
        result = json.loads((folder / 'result.json').read_text())
        self.assertEqual(result['facade_recovery'], dict(release=OLD, restarted=True))
        self.assertEqual(result['actions']['restart previous facade']['exit'], 0)

    def test_failed_facade_bootstrap_restores_the_original_profile_before_restarting(self):
        original_profile = self.layout.tunnel_profile.read_bytes()
        real = self.system.launchctl
        profiles_at_start = []

        def launchctl(*args):
            if args == ('bootstrap', 'gui/501', str(self.facade_plist)):
                profiles_at_start.append(self.layout.tunnel_profile.read_bytes())
                if len(profiles_at_start) == 1:
                    return 5
            return real(*args)

        self.system.launchctl = launchctl
        self.assertEqual(self.run_deploy(), 1)
        self.assertEqual(profiles_at_start, [original_profile.replace(OLD.encode(), NEW.encode()), original_profile])
        self.assertTrue(self.system.loaded(FACADE_LABEL))
        folder, = self.layout.runtime.glob('deploy-*')
        result = json.loads((folder / 'result.json').read_text())
        self.assertIn('start facade: launchctl bootstrap refused', result['error'])
        self.assertEqual(result['actions']['start facade']['exit'], 5)
        self.assertEqual(result['facade_recovery'], dict(release=OLD, restarted=True))

    def test_cli_and_facade_deploy_accepts_an_already_stopped_facade(self):
        self.system.loaded_labels.remove(FACADE_LABEL)
        broker = self.layout.broker_label
        self.layout.broker_plist.write_bytes(self.layout.broker_plist.read_bytes().replace(OLD.encode(), NEW.encode()))
        self.assertEqual(self.run_deploy(components=('cli', 'facade')), 0, self.lines)
        self.assertTrue(self.system.loaded(FACADE_LABEL))
        self.assertEqual(self.system.pid(broker), 100)
        folder, = self.layout.runtime.glob('deploy-*')
        result = json.loads((folder / 'result.json').read_text())
        self.assertEqual(result['actions']['stop facade']['exit'], 0)
        self.assertEqual(result['actions']['stop facade']['bootout_exit'], 3)

    def test_stop_accepts_an_already_stopped_broker(self):
        broker = self.layout.broker_label
        self.system.loaded_labels.remove(broker)
        actions = {}
        deploy.stop(self.system, broker, actions, 'stop broker')
        self.assertEqual(actions['stop broker']['exit'], 0)
        self.assertEqual(actions['stop broker']['bootout_exit'], 3)

    def test_recovery_failure_preserves_both_errors_in_the_receipt(self):
        real = self.system.launchctl

        def launchctl(*args):
            if args[0] == 'bootstrap':
                return 5
            return real(*args)

        self.system.launchctl = launchctl
        self.assertEqual(self.run_deploy(), 1)
        folder, = self.layout.runtime.glob('deploy-*')
        result = json.loads((folder / 'result.json').read_text())
        self.assertIn('start broker: launchctl bootstrap refused', result['error'])
        self.assertIn('restart previous facade: launchctl bootstrap refused', result['facade_recovery']['error'])
        self.assertFalse(result['facade_recovery']['restarted'])
        self.assertEqual(result['actions']['restart previous facade']['exit'], 5)

    def test_an_existing_release_that_differs_from_the_commit_is_refused(self):
        scripts = self.layout.release('nana', NEW) / deploy.SKILL / 'scripts'
        scripts.mkdir(parents=True)
        (scripts / 'bridge.py').write_text('# edited in place\n')
        with self.assertRaisesRegex(deploy.Refusal, 'RELEASE_MISMATCH'):
            self.run_deploy()
        self.assertEqual(self.system.calls, [])


if __name__ == '__main__':
    unittest.main()
