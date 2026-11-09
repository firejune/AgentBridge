#!/usr/bin/env python3
"""Agent worktrees: one root, one way in (new, adopt) and one way out (sweep by live use).

Git state cannot tell whether a worktree is in use, so sweep removes a worktree
only when it is clean, its work has landed on the base branch, its owner
session is not alive, and no live session cwd and no open file is inside it.
Landing is a content test, not ancestry: squash merges never make the branch
an ancestor of the base. This module depends on sessions.py only for live
session discovery, so it can be split out without touching messaging.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sessions

OWNER_FILE = 'OWNER.json'
SIDECAR_FILE = 'SIDECAR.json'
LINKS_FILE = 'workspace-links.json'
ROOT_ENV = 'AGENT_BRIDGE_WT_ROOT'
WORKSPACE_ENV = 'AGENT_BRIDGE_WORKSPACE'
LINKS_ENV = 'AGENT_BRIDGE_WT_LINKS'
LINKS_SCHEMA = 'agent-bridge/wt-links@1'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
PROVIDERS = ('claude', 'codex', 'agy')
# Session ids the harnesses export to their own shells.
SESSION_ENV = (('claude', 'CLAUDE_CODE_SESSION_ID'), ('codex', 'CODEX_THREAD_ID'))
# Codex thread state when the shared app-server observed it; notLoaded is the only dead one.
CODEX_DEAD = {'notLoaded'}


class WorktreeError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def git(cwd, *args, check=True):
    result = subprocess.run(['git', '-C', str(cwd), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, timeout=60)
    if check and result.returncode:
        raise WorktreeError(f'git {" ".join(args)} failed in {cwd}: {result.stderr.strip()[-500:]}')
    return result


def out(cwd, *args):
    return git(cwd, *args).stdout.strip()


def inside(path, folder):
    """True when path is folder or below it (both resolved)."""
    try:
        path = Path(path).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return False
    return path == folder or folder in path.parents


def check_name(kind, value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise WorktreeError(f'{kind} must match {NAME.pattern}: {value!r}')
    return value


def slugify(text):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', text.lower())).strip('-')[:40].strip('-')


# --- Locations ---------------------------------------------------------------

def main_checkout(path):
    """The main working tree of the repository that holds path."""
    common = Path(out(path, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    if common.name != '.git':
        raise WorktreeError(f'{path} belongs to a bare or unusual repository ({common}); not supported')
    return common.parent.resolve()


def workspace_default(cwd=None):
    if os.environ.get(WORKSPACE_ENV):
        return Path(os.environ[WORKSPACE_ENV]).expanduser().resolve()
    cwd = Path(cwd or os.getcwd()).resolve()
    try:
        return main_checkout(cwd).parent
    except (WorktreeError, OSError, subprocess.SubprocessError):
        return cwd


def root_default(workspace):
    return Path(os.environ[ROOT_ENV]).expanduser().resolve() if os.environ.get(ROOT_ENV) else workspace / '.worktrees'


def harness_dirs(repo):
    """Worktree locations owned by an agent harness: read, never moved or removed."""
    return [repo / '.claude' / 'worktrees', Path.home() / '.codex' / 'worktrees']


def location(path, repo, root):
    if path == repo:
        return 'main'
    if inside(path, root):
        return 'root'
    if any(inside(path, h.resolve()) for h in harness_dirs(repo)):
        return 'harness'
    return 'other'


def is_checkout(path):
    return (path / '.git').is_dir()


def scan_repos(workspace, root):
    """Main checkouts directly under the workspace, plus checkouts found under the root."""
    found = []
    for parent in [workspace, *([d for d in root.iterdir() if d.is_dir()] if root.is_dir() else [])]:
        try:
            children = sorted(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.endswith('.ws'):
                continue
            if child.is_dir() and not child.is_symlink() and is_checkout(child):
                found.append(child.resolve())
    return found


def origin_url(repo):
    url = git(repo, 'config', '--get', 'remote.origin.url', check=False).stdout.strip()
    return re.sub(r'(\.git)?/*$', '', re.sub(r'^(git@|ssh://git@|https?://)([^:/]+)[:/]', r'\2/', url)).lower() if url else None


def classify(checkouts):
    """Split checkouts into repos and clones (a second checkout of the same origin)."""
    groups = {}
    for path in checkouts:
        groups.setdefault(origin_url(path) or f'local:{path}', []).append(path)
    repos, clones = [], []
    for url, paths in groups.items():
        named = [p for p in paths if url.rsplit('/', 1)[-1] == p.name.lower()]
        canonical = (named or sorted(paths, key=lambda p: (len(p.name), p.name)))[0]
        repos.append(canonical)
        clones.extend({'path': str(p), 'of': str(canonical), 'origin': None if url.startswith('local:') else url}
                      for p in paths if p != canonical)
    return sorted(repos), sorted(clones, key=lambda c: c['path'])


def worktrees(repo):
    rows, row = [], None
    for line in out(repo, 'worktree', 'list', '--porcelain').splitlines() + ['']:
        if not line:
            if row:
                rows.append(row)
            row = None
            continue
        key, _, value = line.partition(' ')
        if key == 'worktree':
            row = {'path': value}
        elif row is not None:
            row[key] = value or True
    return rows


# --- Ownership ---------------------------------------------------------------
# OWNER.json lives in the worktree's own admin directory (.git/worktrees/<id>/),
# not in its files: it cannot be committed by `git add -A`, never makes the
# tree dirty, is not walked by tools, and goes away with the worktree.

def owner_path(path):
    return Path(out(path, 'rev-parse', '--absolute-git-dir')) / OWNER_FILE


def read_owner(path):
    try:
        data = json.loads(owner_path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, WorktreeError):
        return None
    return data if isinstance(data, dict) else None


def calling_session():
    for provider, name in SESSION_ENV:
        if os.environ.get(name):
            return provider, os.environ[name]
    return None, None


def write_owner(path, agent, issue, repo, purpose, session=None, provider=None, extra=None, force=False):
    target = owner_path(path)
    if target.exists() and not force:
        raise WorktreeError(f'{path} already has an owner record; pass --force to replace it')
    if session is None:
        env_provider, env_session = calling_session()
        if provider in (None, env_provider):
            provider, session = env_provider, env_session
    record = {'agent': agent, 'session': session, 'provider': provider, 'issue': issue, 'repo': repo,
              'created': now(), 'purpose': purpose, **(extra or {})}
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record


def resolve_repo(spec, workspace):
    candidate = Path(spec).expanduser()
    if not candidate.is_dir():
        candidate = workspace / spec
    if not candidate.is_dir():
        raise WorktreeError(f'No repository {spec!r} as a path or under {workspace}')
    return main_checkout(candidate.resolve())


def issue_slug(repo, issue):
    result = subprocess.run(['gh', 'issue', 'view', issue, '--json', 'title', '--jq', '.title'], cwd=repo,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            text=True, timeout=30)
    return slugify(result.stdout) if result.returncode == 0 else ''


def sidecar_path(path):
    path = Path(path)
    return path.with_name(f'{path.name}.ws')


def sidecar_for(path, owner=None):
    path = Path(path)
    owner = read_owner(path) if owner is None else owner
    raw = owner.get('sidecar') if isinstance(owner, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    expected = sidecar_path(path)
    if Path(raw).expanduser().resolve(strict=False) != expected.resolve(strict=False):
        return None
    return expected


def rmtree_sidecar(target):
    target = Path(target)
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
        return
    if not target.exists():
        return
    for root_dir, dirs, files in os.walk(target, topdown=True, followlinks=False):
        root_p = Path(root_dir)
        try:
            os.chmod(root_p, os.lstat(root_p).st_mode | 0o700)
        except OSError:
            pass
        for name in files:
            fpath = root_p / name
            if not fpath.is_symlink():
                try:
                    os.chmod(fpath, os.lstat(fpath).st_mode | 0o600)
                except OSError:
                    pass
    shutil.rmtree(target)


def lock_peer_files(peer_dir):
    for root_dir, dirs, files in os.walk(peer_dir, topdown=True, followlinks=False):
        if 'node_modules' in dirs:
            dirs.remove('node_modules')
        root_p = Path(root_dir)
        for name in files:
            fpath = root_p / name
            if not fpath.is_symlink():
                st = os.lstat(fpath)
                os.chmod(fpath, st.st_mode & ~0o222)


def check_member_key(key):
    if (not isinstance(key, str) or not key or key in ('.', '..')
            or key.startswith(('/', '~')) or '/' in key or '\\' in key
            or not NAME.fullmatch(key)):
        raise WorktreeError(
            f'WT_LINK_PROTECTED_MAIN_REFUSED: invalid member key {key!r}; '
            f'must be a single checkout segment matching {NAME.pattern}'
        )
    return key


def parse_peers(peers):
    if not peers:
        return {}
    if isinstance(peers, dict):
        items = list(peers.items())
    else:
        items = []
        for item in peers:
            if not isinstance(item, str) or '=' not in item:
                raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: --peer must be REPO=REF, got {item!r}')
            k, _, v = item.partition('=')
            items.append((k.strip(), v.strip()))
    out_peers = {}
    for k, v in items:
        if not k or not v:
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: --peer requires non-empty REPO=REF, got {k!r}={v!r}')
        check_member_key(k)
        out_peers[k] = v
    return out_peers


def load_links_config(root, links_config=None, no_link=False):
    if no_link:
        return None
    if isinstance(links_config, dict):
        data = links_config
    else:
        explicit = links_config if links_config is not None else os.environ.get(LINKS_ENV)
        if explicit is not None:
            cfg_path = Path(explicit).expanduser().resolve()
            if not cfg_path.is_file():
                raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: links config {cfg_path} does not exist')
        else:
            cfg_path = Path(root) / LINKS_FILE
            if not cfg_path.exists() and not cfg_path.is_symlink():
                return None
        try:
            data = json.loads(cfg_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: invalid links config {cfg_path}: {error}') from error
    if not isinstance(data, dict):
        raise WorktreeError('WT_LINK_DECLARATION_MISMATCH: links config must be a JSON object')
    if 'workspaces' in data:
        raise WorktreeError('WT_LINK_PROTECTED_MAIN_REFUSED: links config must declare members, not direct shared workspaces')
    schema = data.get('schema', LINKS_SCHEMA)
    if schema != LINKS_SCHEMA:
        raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: unsupported links schema {schema!r}')
    linker = data.get('linker', 'isolated')
    if linker != 'isolated':
        raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: linker must be "isolated", got {linker!r}')
    members = data.get('members')
    if not isinstance(members, dict) or not members:
        raise WorktreeError('WT_LINK_DECLARATION_MISMATCH: links config must declare a non-empty members mapping')
    return data


def version_spec_matches(spec, expected_version):
    if not isinstance(spec, str):
        return False
    spec = spec.strip()
    if spec in ('workspace:*', 'workspace:^', 'workspace:~'):
        return True
    if spec.startswith('workspace:'):
        spec = spec.removeprefix('workspace:').strip()
    if spec[:1] in ('^', '~'):
        spec = spec[1:].strip()
    return spec == expected_version


def ref_points_to_checkout(ref, workspace, main_repos):
    if not isinstance(ref, str) or not ref.strip():
        return False
    if ref.startswith(('/', './', '../', '~')):
        return True
    for candidate in (Path(ref).expanduser(), Path(workspace) / ref):
        try:
            if candidate.exists() or candidate.is_symlink():
                resolved = candidate.resolve(strict=False)
                if candidate.is_dir() or any(inside(resolved, mr) for mr in main_repos):
                    return True
        except OSError:
            continue
    return False


def validate_sidecar_destinations(path, ws_path, peer_keys, all_main_repos):
    path_resolved = path.resolve(strict=False)
    ws_resolved = ws_path.resolve(strict=False)
    peers_resolved = (ws_path / 'peers').resolve(strict=False)
    for target_p in (path_resolved, ws_resolved, peers_resolved):
        if any(inside(target_p, mr) or inside(mr, target_p) for mr in all_main_repos):
            raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: {target_p} overlaps a protected main checkout')
    for key in peer_keys:
        check_member_key(key)
        peer_dest = (ws_path / 'peers' / key).resolve(strict=False)
        if peer_dest.parent != peers_resolved or not inside(peer_dest, peers_resolved) or peer_dest == peers_resolved:
            raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: peer destination {peer_dest} escapes {peers_resolved}')
        if any(inside(peer_dest, mr) or inside(mr, peer_dest) for mr in all_main_repos):
            raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: peer destination {peer_dest} overlaps a protected main checkout')


def prepare_sidecar_plan(repo, start_ref, workspace, path, ws_path, config, peer_overrides):
    if ws_path.exists() or ws_path.is_symlink():
        raise WorktreeError(f'WT_LINK_SIDECAR_EXISTS: {ws_path} already exists; not reusing it')
    members = config['members']
    discovered_main = {repo.resolve()}
    try:
        for child in Path(workspace).iterdir():
            if not child.name.endswith('.ws') and child.is_dir() and not child.is_symlink() and is_checkout(child):
                discovered_main.add(child.resolve())
    except OSError:
        pass
    member_repos = {}
    for key, member in members.items():
        check_member_key(key)
        if not isinstance(member, dict):
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: member {key!r} must be an object')
        if any(member.get(flag) for flag in ('use_main_checkout', 'link_main', 'workspace', 'workspace_path', 'target')) \
                or member.get('mode') in ('main', 'symlink', 'facade', 'shared') \
                or member.get('strategy') in ('main', 'symlink', 'facade', 'shared'):
            raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: member {key} cannot link shared main checkouts or symlink facades')
        name, version = member.get('name'), member.get('version')
        if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version.strip():
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: member {key} must declare non-empty name and version')
        spec = member.get('path') or key
        if not isinstance(spec, str) or not spec.strip():
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: member {key} has invalid path {spec!r}')
        resolved_member_repo = resolve_repo(spec, workspace)
        if resolved_member_repo.name != key:
            raise WorktreeError(
                f'WT_LINK_PROTECTED_MAIN_REFUSED: member key {key!r} does not match checkout identity {resolved_member_repo.name!r}'
            )
        member_repos[key] = resolved_member_repo

    all_main_repos = discovered_main | {r.resolve() for r in member_repos.values()}
    validate_sidecar_destinations(path, ws_path, list(members.keys()), all_main_repos)

    for peer_key, peer_ref in peer_overrides.items():
        check_member_key(peer_key)
        if ref_points_to_checkout(peer_ref, workspace, all_main_repos):
            raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: --peer {peer_key}={peer_ref!r} points to a checkout directory; pass a git commit ref')
        if peer_key not in members:
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: unknown peer repository {peer_key!r}')
        if peer_key == repo.name:
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: cannot override primary repository {repo.name!r} via --peer')

    declared_by_name = {m['name']: (k, m) for k, m in members.items()}
    forbidden_legacy = {alias: k for k, m in members.items() for alias in (m.get('legacy_names') or ()) if isinstance(alias, str)}
    condition = config.get('condition')
    if condition is not None and (not isinstance(condition, str) or not condition.strip()):
        raise WorktreeError('WT_LINK_SOURCE_CONDITION_MISSING: condition must be a non-empty string when present')

    check_list = [(repo.name, members[repo.name], repo, start_ref, False)]
    for key, member in members.items():
        if key == repo.name:
            continue
        if member.get('leaf') and key not in peer_overrides:
            continue
        ref = peer_overrides.get(key, member.get('ref', 'origin/main'))
        if not isinstance(ref, str) or not ref.strip():
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: missing git ref for {key}')
        if ref_points_to_checkout(ref, workspace, all_main_repos):
            raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: ref {ref!r} for {key} points to a checkout directory; pass a git commit ref')
        check_list.append((key, member, member_repos[key], ref, True))

    peers = []
    for m_key, m_cfg, m_repo, m_ref, is_peer in check_list:
        rev = git(m_repo, 'rev-parse', '--verify', '--quiet', f'{m_ref}^{{commit}}', check=False)
        if rev.returncode != 0:
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: cannot resolve commit for {m_key} at ref {m_ref!r}')
        commit = rev.stdout.strip()
        shown = git(m_repo, 'show', f'{commit}:package.json', check=False)
        if shown.returncode != 0:
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: {m_key} at {commit[:12]} is missing package.json')
        try:
            pkg = json.loads(shown.stdout)
        except ValueError as error:
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: {m_key} at {commit[:12]} has invalid package.json: {error}') from error
        if not isinstance(pkg, dict):
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: {m_key} at {commit[:12]} package.json must be an object')
        if (pkg.get('name') != m_cfg['name']
                or pkg.get('version') != m_cfg['version']
                or bool(pkg.get('private', False)) != bool(m_cfg.get('private', False))):
            raise WorktreeError(
                f'WT_LINK_DECLARATION_MISMATCH: {m_key} at {commit[:12]} has '
                f'name={pkg.get("name")!r} version={pkg.get("version")!r} private={bool(pkg.get("private", False))}, '
                f'expected name={m_cfg["name"]!r} version={m_cfg["version"]!r} private={bool(m_cfg.get("private", False))}'
            )
        for sec in ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
            deps = pkg.get(sec)
            if not isinstance(deps, dict):
                continue
            for dep_name, dep_spec in deps.items():
                if dep_name in forbidden_legacy:
                    raise WorktreeError(
                        f'WT_LINK_DECLARATION_MISMATCH: {m_key} {sec}[{dep_name!r}] uses unaligned legacy package name for {forbidden_legacy[dep_name]}'
                    )
                if dep_name in declared_by_name:
                    target_key, target_cfg = declared_by_name[dep_name]
                    if not version_spec_matches(dep_spec, target_cfg['version']):
                        raise WorktreeError(
                            f'WT_LINK_DECLARATION_MISMATCH: {m_key} {sec}[{dep_name!r}] = {dep_spec!r} disagrees with {target_key} version {target_cfg["version"]!r}'
                        )
        cond_target = None
        if m_cfg.get('requires_source_condition'):
            if not condition:
                raise WorktreeError(f'WT_LINK_SOURCE_CONDITION_MISSING: {m_key} requires source condition, but config condition is unset')
            exports = pkg.get('exports')
            dot = exports.get('.') if isinstance(exports, dict) else None
            cond_entry = dot.get(condition) if isinstance(dot, dict) else None
            if isinstance(cond_entry, dict):
                cond_target = cond_entry.get('default') or cond_entry.get('import') or cond_entry.get('types')
            else:
                cond_target = cond_entry
            if not isinstance(cond_target, str) or not cond_target.strip():
                raise WorktreeError(f'WT_LINK_SOURCE_CONDITION_MISSING: {m_key} at {commit[:12]} is missing exports["."][{condition!r}]')
            rel_target = cond_target.removeprefix('./').lstrip('/')
            if not rel_target or git(m_repo, 'cat-file', '-e', f'{commit}:{rel_target}', check=False).returncode != 0:
                raise WorktreeError(
                    f'WT_LINK_SOURCE_CONDITION_MISSING: {m_key} at {commit[:12]} export target {cond_target!r} does not exist in commit tree'
                )
        if is_peer:
            peers.append({
                'repo': m_key,
                'name': m_cfg['name'],
                'version': m_cfg['version'],
                'private': bool(m_cfg.get('private', False)),
                'ref': m_ref,
                'commit': commit,
                'repo_path': m_repo,
                'requires_source_condition': bool(m_cfg.get('requires_source_condition', False)),
                'cond_target': cond_target,
            })

    return {
        'primary': members[repo.name],
        'condition': condition,
        'linker': config.get('linker', 'isolated'),
        'peers': peers,
        'declared_by_name': declared_by_name,
        'all_main_repos': all_main_repos,
    }


def materialize_peer(peer_repo, commit, dest_dir):
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    cmd = ['git', '-C', str(peer_repo), 'archive', '--format=tar', commit]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    try:
        with tarfile.open(fileobj=proc.stdout, mode='r|*') as tar:
            for member in tar:
                member_path = (dest_dir / member.name).resolve(strict=False)
                if not inside(member_path, dest_resolved):
                    raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: archive entry {member.name!r} escapes {dest_dir}')
                if member.issym() or member.islnk():
                    link_target = (member_path.parent / member.linkname).resolve(strict=False)
                    if not inside(link_target, dest_resolved):
                        raise WorktreeError(
                            f'WT_LINK_PROTECTED_MAIN_REFUSED: archive symlink {member.name!r} -> {member.linkname!r} escapes {dest_dir}'
                        )
                try:
                    tar.extract(member, path=str(dest_dir), set_attrs=True, filter='data')
                except TypeError:
                    tar.extract(member, path=str(dest_dir), set_attrs=True)
        _, stderr = proc.communicate(timeout=60)
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    if proc.returncode:
        err_text = stderr.decode('utf-8', 'replace').strip()[-500:]
        raise WorktreeError(f'git archive failed in {peer_repo} for {commit[:12]}: {err_text}')


def verify_no_main_links(path, ws_path, all_main_repos):
    roots = [ws_path]
    lane_nm = path / 'node_modules'
    if lane_nm.exists() or lane_nm.is_symlink():
        roots.append(lane_nm)
    for scan_root in roots:
        if scan_root.is_symlink():
            resolved = scan_root.resolve(strict=False)
            if any(inside(resolved, mr) for mr in all_main_repos):
                raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: {scan_root} resolves into protected main checkout {resolved}')
            continue
        for root_dir, dirs, files in os.walk(scan_root, topdown=True, followlinks=False):
            root_p = Path(root_dir)
            for name in [*dirs, *files]:
                entry = root_p / name
                if entry.is_symlink():
                    resolved = entry.resolve(strict=False)
                    if any(inside(resolved, mr) for mr in all_main_repos):
                        raise WorktreeError(
                            f'WT_LINK_PROTECTED_MAIN_REFUSED: symlink {entry} resolves into protected main checkout {resolved}'
                        )


def verify_postinstall_bindings(path, ws_path, plan):
    peers_dir = (ws_path / 'peers').resolve()
    member_dirs = [
        (plan['primary']['name'], path.resolve()),
        *((peer['name'], (peers_dir / peer['repo']).resolve()) for peer in plan['peers']),
    ]
    expected_by_pkg = dict(member_dirs)
    peer_by_pkg = {peer['name']: peer for peer in plan['peers']}
    declared_by_name = plan.get('declared_by_name') or {}

    for pkg_name, member_dir in member_dirs:
        pkg_file = member_dir / 'package.json'
        try:
            pkg_data = json.loads(pkg_file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: cannot read {pkg_file}: {error}') from error
        for sec in ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
            deps = pkg_data.get(sec)
            if not isinstance(deps, dict):
                continue
            for dep_name in deps:
                if dep_name not in declared_by_name:
                    continue
                if dep_name not in expected_by_pkg:
                    raise WorktreeError(
                        f'WT_LINK_DECLARATION_MISMATCH: {pkg_name} declares workspace dependency {dep_name!r}, '
                        f'which is not materialized in {ws_path}'
                    )
                expected_target = expected_by_pkg[dep_name]
                binding = member_dir / 'node_modules' / Path(*dep_name.split('/'))
                if not binding.exists() and not binding.is_symlink():
                    raise WorktreeError(
                        f'WT_LINK_DECLARATION_MISMATCH: missing post-install workspace binding {binding} -> {expected_target}'
                    )
                if not binding.is_symlink():
                    raise WorktreeError(
                        f'WT_LINK_DECLARATION_MISMATCH: post-install binding {binding} is not a symlink '
                        f'(registry fallback or copy refused)'
                    )
                resolved_target = binding.resolve(strict=False)
                if any(inside(resolved_target, mr) for mr in plan['all_main_repos']):
                    raise WorktreeError(
                        f'WT_LINK_PROTECTED_MAIN_REFUSED: post-install binding {binding} resolves into '
                        f'protected main checkout {resolved_target}'
                    )
                if not binding.exists() or resolved_target != expected_target:
                    raise WorktreeError(
                        f'WT_LINK_DECLARATION_MISMATCH: post-install binding {binding} resolves to {resolved_target}, '
                        f'expected owned workspace member {expected_target}'
                    )
                peer_info = peer_by_pkg.get(dep_name)
                if peer_info and peer_info.get('requires_source_condition') and peer_info.get('cond_target'):
                    rel_target = peer_info['cond_target'].removeprefix('./').lstrip('/')
                    bound_entry = binding / rel_target
                    if not bound_entry.exists() or not inside(bound_entry.resolve(strict=False), expected_target):
                        raise WorktreeError(
                            f'WT_LINK_SOURCE_CONDITION_MISSING: bound source entry {bound_entry} missing or escapes {expected_target}'
                        )


def provision_sidecar(path, ws_path, repo, agent, issue, head_sha, plan):
    peer_keys = [peer['repo'] for peer in plan['peers']]
    validate_sidecar_destinations(path, ws_path, peer_keys, plan['all_main_repos'])
    workspace_entries = [f'../{path.name}', *(f'peers/{peer["repo"]}' for peer in plan['peers'])]
    path_resolved = path.resolve()
    peers_dir = ws_path / 'peers'
    peers_resolved = peers_dir.resolve(strict=False)
    for rel_entry in workspace_entries:
        target = (ws_path / rel_entry).resolve(strict=False)
        if any(inside(target, mr) or inside(mr, target) for mr in plan['all_main_repos']):
            raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: workspace entry {rel_entry!r} resolves into protected main checkout')
        if target != path_resolved and (target.parent != peers_resolved or not inside(target, peers_resolved)):
            raise WorktreeError(f'WT_LINK_PROTECTED_MAIN_REFUSED: workspace entry {rel_entry!r} escapes sidecar peers')

    peers_dir.mkdir(parents=True, exist_ok=False)
    linked_peers = {}
    for peer in plan['peers']:
        peer_dir = peers_dir / peer['repo']
        materialize_peer(peer['repo_path'], peer['commit'], peer_dir)
        verify_no_main_links(path, ws_path, plan['all_main_repos'])
        linked_peers[peer['repo']] = {
            'repo': peer['repo'],
            'name': peer['name'],
            'version': peer['version'],
            'private': peer['private'],
            'ref': peer['ref'],
            'commit': peer['commit'],
            'path': str(peer_dir),
        }

    (ws_path / 'bunfig.toml').write_text('[install]\nlinker = "isolated"\n', encoding='utf-8')
    ws_pkg = {
        'name': f'wt-sidecar-{repo.name}-{agent}-{issue}',
        'private': True,
        'workspaces': workspace_entries,
    }
    (ws_path / 'package.json').write_text(json.dumps(ws_pkg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    try:
        res = subprocess.run(['bun', 'install'], cwd=str(ws_path), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise WorktreeError(f'WT_LINK_INSTALL_FAILED: bun install failed in {ws_path}: {error}') from error
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or '').strip()[-500:]
        raise WorktreeError(f'WT_LINK_INSTALL_FAILED: bun install exited {res.returncode} in {ws_path}: {detail}')

    if dirty_count(path) != 0:
        raise WorktreeError(f'WT_LINK_INSTALL_FAILED: bun install dirtied tracked or untracked files in {path}')

    verify_no_main_links(path, ws_path, plan['all_main_repos'])
    verify_postinstall_bindings(path, ws_path, plan)
    for peer in plan['peers']:
        lock_peer_files(peers_dir / peer['repo'])

    condition = plan['condition']
    source_command = f'bun --conditions={condition}' if condition else None
    return {
        'schema': LINKS_SCHEMA,
        'worktree': str(path),
        'sidecar': str(ws_path),
        'repo': repo.name,
        'name': plan['primary']['name'],
        'version': plan['primary']['version'],
        'head': head_sha,
        'linker': plan['linker'],
        'condition': condition,
        'source_command': source_command,
        'linked_peers': linked_peers,
        'created': now(),
    }


def write_sidecar_meta(ws_path, sidecar_meta, owner_record):
    payload = {**sidecar_meta, 'owner': owner_record}
    (Path(ws_path) / SIDECAR_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return payload


def read_sidecar(ws_path):
    try:
        data = json.loads((Path(ws_path) / SIDECAR_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def valid_sidecar_meta(meta, lane_path, ws_path, repo_name=None):
    if not isinstance(meta, dict) or meta.get('schema') != LINKS_SCHEMA:
        return False
    if repo_name is not None and meta.get('repo') != repo_name:
        return False
    if not isinstance(meta.get('owner'), dict):
        return False
    try:
        worktree_match = Path(meta.get('worktree', '')).resolve(strict=False) == Path(lane_path).resolve(strict=False)
        sidecar_match = Path(meta.get('sidecar', '')).resolve(strict=False) == Path(ws_path).resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return False
    return worktree_match and sidecar_match


def rollback_worktree(repo, path, ws_path, branch, owner_meta=None):
    if ws_path is not None and (ws_path.exists() or ws_path.is_symlink()):
        rmtree_sidecar(ws_path)
    if path.exists() or path.is_symlink():
        nm = path / 'node_modules'
        if nm.exists() or nm.is_symlink():
            rmtree_sidecar(nm)
        dirty = dirty_count(path)
        if dirty == 0:
            rem = git(repo, 'worktree', 'remove', str(path), check=False)  # Never pass --force.
            if rem.returncode == 0:
                if branch:
                    git(repo, 'branch', '-D', branch, check=False)
                return {'rolled_back': True, 'retained': False}
        if isinstance(owner_meta, dict):
            try:
                write_owner(
                    path,
                    owner_meta['agent'],
                    owner_meta['issue'],
                    repo.name,
                    owner_meta['purpose'],
                    owner_meta.get('session'),
                    owner_meta.get('provider'),
                    {'branch': branch, 'base': owner_meta.get('base'), 'head': owner_meta.get('head'),
                     'rollback_retained_dirty': True},
                    force=True,
                )
            except (WorktreeError, OSError):
                pass
        return {'rolled_back': False, 'retained': True, 'dirty': dirty}
    if branch:
        git(repo, 'branch', '-D', branch, check=False)
    return {'rolled_back': True, 'retained': False}


def new(repo_spec, agent, issue, base='main', detach=None, slug=None, session=None, provider=None,
        workspace=None, root=None, slugger=issue_slug, links_config=None, no_link=False, peers=None):
    agent, issue = check_name('AGENT', agent), check_name('ISSUE', issue)
    workspace = Path(workspace).resolve() if workspace else workspace_default()
    root = Path(root).resolve() if root else root_default(workspace)
    repo = resolve_repo(repo_spec, workspace)
    path = root / repo.name / f'{agent}-{issue}'
    ws_path = sidecar_path(path)
    if path.exists() or path.is_symlink():
        raise WorktreeError(f'{path} already exists; not reusing it')
    peer_overrides = parse_peers(peers)
    config = load_links_config(root, links_config=links_config, no_link=no_link)
    active_link = bool(config and repo.name in config['members'])
    if peer_overrides and not active_link:
        raise WorktreeError(f'WT_LINK_DECLARATION_MISMATCH: --peer requires active link configuration for {repo.name}')
    if detach:
        branch, start, purpose = None, detach, 'pin'
    else:
        start = f'origin/{base}' if git(repo, 'rev-parse', '--verify', '--quiet', f'origin/{base}^{{commit}}',
                                        check=False).returncode == 0 else base
        tail = slugify(slug) if slug is not None else slugger(repo, issue)
        branch = f'{agent}/{issue}' + (f'-{tail}' if tail else '')
        if git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0:
            raise WorktreeError(f'Branch {branch} already exists; not reusing it')
        purpose = 'work'
    plan = prepare_sidecar_plan(repo, start, workspace, path, ws_path, config, peer_overrides) if active_link else None
    path.parent.mkdir(parents=True, exist_ok=True)
    if detach:
        git(repo, 'worktree', 'add', '--detach', str(path), detach)
    else:
        git(repo, 'worktree', 'add', '--no-track', '-b', branch, str(path), start)
    if not active_link:
        record = write_owner(path, agent, issue, repo.name, purpose, session, provider,
                             {'branch': branch, 'base': start, 'head': out(path, 'rev-parse', 'HEAD')})
        return {'created': True, 'path': str(path), 'branch': branch, 'owner': record}
    head_sha = None
    try:
        head_sha = out(path, 'rev-parse', 'HEAD')
        sidecar_info = provision_sidecar(path, ws_path, repo, agent, issue, head_sha, plan)
        record = write_owner(path, agent, issue, repo.name, purpose, session, provider,
                             {'branch': branch, 'base': start, 'head': head_sha,
                              'sidecar': str(ws_path), 'linker': sidecar_info['linker'],
                              'condition': sidecar_info['condition'],
                              'source_command': sidecar_info['source_command'],
                              'linked_peers': sidecar_info['linked_peers']})
        write_sidecar_meta(ws_path, sidecar_info, record)
    except BaseException as error:
        rb = rollback_worktree(
            repo, path, ws_path, branch,
            owner_meta={'agent': agent, 'issue': issue, 'purpose': purpose,
                        'session': session, 'provider': provider, 'base': start, 'head': head_sha},
        )
        if rb.get('retained') and isinstance(error, WorktreeError):
            raise WorktreeError(
                f'{error} (WT_LINK_ROLLBACK_REFUSED_DIRTY: retained dirty worktree {path} on branch {branch}; '
                f'removed sidecar {ws_path} and derived node_modules without --force)'
            ) from error
        raise
    return {'created': True, 'path': str(path), 'branch': branch, 'owner': record,
            'sidecar': str(ws_path), 'condition': sidecar_info['condition'],
            'source_command': sidecar_info['source_command'],
            'linked_peers': sidecar_info['linked_peers']}


def adopt(path, agent, issue, purpose='work', session=None, provider=None, force=False):
    agent, issue = check_name('AGENT', agent), check_name('ISSUE', issue)
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise WorktreeError(f'{path} is not a directory')
    repo = main_checkout(path)
    if Path(out(path, 'rev-parse', '--show-toplevel')).resolve() != path:
        raise WorktreeError(f'{path} is not the top of a worktree')
    if path == repo:
        raise WorktreeError(f'{path} is the main checkout; only linked worktrees get an owner')
    branch = git(path, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False).stdout.strip() or None
    head_sha = out(path, 'rev-parse', 'HEAD')
    extra = {'branch': branch, 'head': head_sha, 'adopted': True}
    ws_candidate = sidecar_path(path)
    meta = read_sidecar(ws_candidate)
    if isinstance(meta, dict) and meta.get('schema') == LINKS_SCHEMA:
        extra['sidecar'] = str(ws_candidate)
    record = write_owner(path, agent, issue, repo.name, purpose, session, provider, extra, force=force)
    if isinstance(meta, dict) and meta.get('schema') == LINKS_SCHEMA:
        write_sidecar_meta(ws_candidate, meta, record)
    return {'adopted': True, 'path': str(path), 'owner': record}


# --- Live use ----------------------------------------------------------------

def live_sessions(providers=PROVIDERS):
    """Sessions that may be alive, each with a state: alive or unknown. Errors block removal."""
    rows, errors = [], {}
    if 'claude' in providers:
        for record in sessions.claude_live_records(sessions.claude_sessions_dir()):
            rows.append({'provider': 'claude', 'id': record['sessionId'], 'cwd': record.get('cwd'),
                         'pid': record['pid'], 'state': 'alive'})
    others = [p for p in ('codex', 'agy') if p in providers]
    for provider in others:
        found = sessions.discover(provider, limit=1000)
        if provider in found['errors']:
            errors[provider] = found['errors'][provider]
            continue
        for row in found['providers'][provider]['sessions']:
            if provider == 'codex' and row.get('runtime_observed') and row.get('status') in CODEX_DEAD:
                continue
            state = 'alive' if row.get('runtime_observed') else 'unknown'
            rows.append({'provider': provider, 'id': row['id'], 'cwd': row.get('cwd'), 'state': state})
    return rows, errors


def open_files(skip=()):
    """Every (pid, command, path) the user's processes hold open, cwd included. One lsof call."""
    process = subprocess.Popen(['lsof', '-nP', '-w', '-F', 'pcn'], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True)
    try:
        stdout, _ = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    # lsof exits 1 when it could not read some process; the rest is still valid.
    if process.returncode and not stdout:
        raise WorktreeError(f'lsof failed with exit code {process.returncode}')
    skip = {*skip, process.pid}
    rows, pid, cmd = [], None, None
    for line in stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == 'p':
            pid, cmd = int(value), None
        elif tag == 'c':
            cmd = value
        elif tag == 'n' and value.startswith('/') and pid not in skip:
            rows.append((pid, cmd, value.split(' (', 1)[0]))
    return rows


# --- Assessment --------------------------------------------------------------

def gh_pulls(repo):
    """PRs of the repository by head branch, or an error string. The only network call."""
    try:
        result = subprocess.run(['gh', 'pr', 'list', '--state', 'all', '--limit', '1000', '--json',
                                 'number,state,url,headRefName,headRefOid,mergedAt'], cwd=repo, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    if result.returncode:
        return result.stderr.strip()[-300:] or f'gh exited {result.returncode}'
    pulls = {}
    for item in json.loads(result.stdout):
        pulls.setdefault(item['headRefName'], []).append(item)
    return pulls


def pr_state(pulls, branch, head=None):
    if isinstance(pulls, str):
        return {'state': 'unknown', 'error': pulls}
    found = pulls.get(branch or '', [])
    for state in ('OPEN', 'MERGED', 'CLOSED'):
        # Of several merged PRs from one branch name, prefer the one whose head is this HEAD.
        hits = sorted((p for p in found if p['state'] == state), key=lambda p: p.get('headRefOid') != head)
        if hits:
            return {'state': state.lower(), 'number': hits[0]['number'], 'url': hits[0]['url'],
                    'head': hits[0].get('headRefOid')}
    return {'state': 'none'}


def base_ref(repo, base=None):
    names = [f'origin/{base}'] if base else []
    head = git(repo, 'symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD', check=False).stdout.strip()
    names += [n for n in (head, 'origin/main', 'origin/master') if n]
    return next((n for n in names if git(repo, 'rev-parse', '--verify', '--quiet', f'{n}^{{commit}}',
                                         check=False).returncode == 0), None)


def content_landed(path, base, head='HEAD'):
    """Every path the branch changed (merge-base to tip) is identical on base."""
    fork = git(path, 'merge-base', base, head, check=False)
    if fork.returncode:
        return False, f'no merge-base with {base}'
    # -z: names arrive verbatim (no C-quoting of non-ASCII, spaces or quotes); :(literal) stops glob magic.
    names = git(path, 'diff', '-z', '--no-renames', '--name-only', fork.stdout.strip(), head).stdout
    changed = [p for p in names.split('\0') if p]
    if not changed:
        return True, 'no changes beyond the merge-base'
    same = git(path, 'diff', '--quiet', '--no-renames', head, base, '--',
               *(f':(literal){p}' for p in changed), check=False).returncode == 0
    return same, (f'{len(changed)} changed paths equal {base}' if same else f'changed paths differ from {base}')


def dirty_count(path):
    status = git(path, 'status', '--porcelain', '--untracked-files=all', check=False)
    return None if status.returncode else len([l for l in status.stdout.splitlines() if l])


def last_touched(path):
    stamps = []
    for probe in (Path(out(path, 'rev-parse', '--absolute-git-dir')) / name for name in ('index', 'HEAD', 'logs/HEAD')):
        try:
            stamps.append(probe.stat().st_mtime)
        except OSError:
            pass
    return datetime.fromtimestamp(max(stamps), timezone.utc).isoformat(timespec='seconds') if stamps else None


def assess(repo, entry, root, live, holders, pulls, base, blockers):
    path = Path(entry['path'])
    resolved = path.resolve() if path.exists() else path
    branch = entry['branch'].removeprefix('refs/heads/') if isinstance(entry.get('branch'), str) else None
    row = {'path': str(path), 'repo': repo.name, 'location': location(resolved, repo, root),
           'branch': branch, 'detached': bool(entry.get('detached')), 'head': (entry.get('HEAD') or '')[:12]}
    reasons = []
    if row['location'] == 'main':
        reasons = ['main checkout']
        expected = base.split('/', 1)[1] if base else None
        if expected and branch != expected:  # The main checkout stays on the base branch (#38 rule 1).
            reasons.append(f'main checkout is on {branch or "a detached HEAD"}, not {expected}')
        return {**row, 'action': 'keep', 'reasons': reasons}
    if not path.is_dir():
        return {**row, 'action': 'keep', 'reasons': ['directory is missing (git worktree prune clears it)']}
    if row['location'] == 'harness':
        reasons.append('harness-owned location; left to its harness')
    if entry.get('locked'):
        reasons.append('locked')
    owner = read_owner(path)
    row['owner'] = owner
    sidecar = sidecar_for(path, owner)
    if sidecar is not None:
        row['sidecar'] = str(sidecar)
    if owner is None:
        reasons.append(f'no {OWNER_FILE}; adopt it to make it sweepable')
    row['dirty'] = dirty_count(path)
    if row['dirty'] is None:
        reasons.append('git status failed')
    elif row['dirty']:
        reasons.append(f'dirty: {row["dirty"]} modified or untracked files')
    row['pr'] = pr_state(pulls, branch, entry.get('HEAD')) if branch else {'state': 'none'}
    if row['pr']['state'] == 'open':
        reasons.append(f'PR #{row["pr"]["number"]} is open')
    elif row['pr']['state'] == 'closed':
        reasons.append(f'PR #{row["pr"]["number"]} was closed without merging')
    merged_here = row['pr']['state'] == 'merged' and row['pr'].get('head') == entry.get('HEAD')
    if merged_here:
        row['landed'], row['landed_by'] = True, f'PR #{row["pr"]["number"]} merged at this HEAD'
    elif row['pr']['state'] in ('none', 'unknown', 'merged'):
        # A merged PR that does not cover HEAD (commits after the merge) falls back to the content test.
        if base:
            row['landed'], row['landed_by'] = content_landed(path, base)
        else:
            row['landed'], row['landed_by'] = False, 'no origin base branch to compare with'
        if not row['landed']:
            reasons.append(f'not landed: {row["landed_by"]}')
    else:
        row['landed'] = False
    sid = owner.get('session') if owner else None
    owner_live = [s for s in live if sid and s['id'] == sid]
    if owner_live:
        s = owner_live[0]
        reasons.append(f'owner session {s["provider"]}:{sid} is ' + ('alive' if s['state'] == 'alive'
                                                                       else 'possibly alive (runtime unknown)'))
    row['in_use_by'] = [{'provider': s['provider'], 'id': s['id'], 'cwd': s['cwd'], 'state': s['state']}
                        for s in live if s['cwd'] and inside(s['cwd'], resolved)]
    if row['in_use_by']:
        reasons.append(f'{len(row["in_use_by"])} live session cwd inside')
    row['open_by'] = [{'pid': pid, 'command': cmd} for pid, cmd in
                      sorted({(pid, cmd) for pid, cmd, name in holders if inside(name, resolved)})]
    if row['open_by']:
        reasons.append(f'{len(row["open_by"])} processes have files or cwd inside')
    reasons.extend(blockers)
    row['last_touched'] = last_touched(path)
    return {**row, 'action': 'keep' if reasons else 'remove', 'reasons': reasons}


def assess_orphan_sidecar(repo, ws_path, root, live, pulls, base, blockers):
    lane_path = ws_path.with_name(ws_path.name[:-3])
    ws_resolved = ws_path.resolve(strict=False)
    meta = read_sidecar(ws_path)
    valid = valid_sidecar_meta(meta, lane_path, ws_path, repo.name)
    owner = meta['owner'] if valid else None
    branch = owner.get('branch') if isinstance(owner, dict) and isinstance(owner.get('branch'), str) else None
    head_full = ''
    if valid:
        if branch and git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}^{{commit}}', check=False).returncode == 0:
            head_full = out(repo, 'rev-parse', f'refs/heads/{branch}^{{commit}}')
        else:
            head_full = owner.get('head') or meta.get('head') or ''
    row = {
        'path': str(ws_path),
        'worktree': str(lane_path),
        'sidecar': str(ws_path),
        'sidecar_only': True,
        'repo': repo.name,
        'location': location(ws_resolved, repo, root),
        'branch': branch,
        'detached': not bool(branch),
        'head': head_full[:12],
        'owner': owner,
        'dirty': 0,
    }
    reasons = []
    if row['location'] == 'harness':
        reasons.append('harness-owned location; left to its harness')
    if not valid:
        reasons.append(f'no {SIDECAR_FILE} owner record; arbitrary .ws directory is never removed')
        return {**row, 'pr': {'state': 'none'}, 'landed': False, 'in_use_by': [], 'open_by': [],
                'last_touched': None, 'action': 'keep', 'reasons': reasons}
    row['pr'] = pr_state(pulls, branch, head_full) if branch else {'state': 'none'}
    if row['pr']['state'] == 'open':
        reasons.append(f'PR #{row["pr"]["number"]} is open')
    elif row['pr']['state'] == 'closed':
        reasons.append(f'PR #{row["pr"]["number"]} was closed without merging')
    merged_here = row['pr']['state'] == 'merged' and row['pr'].get('head') == head_full
    if merged_here:
        row['landed'], row['landed_by'] = True, f'PR #{row["pr"]["number"]} merged at this HEAD'
    elif row['pr']['state'] in ('none', 'unknown', 'merged'):
        if base and head_full and git(repo, 'rev-parse', '--verify', '--quiet', f'{head_full}^{{commit}}', check=False).returncode == 0:
            row['landed'], row['landed_by'] = content_landed(repo, base, head=head_full)
        elif not base:
            row['landed'], row['landed_by'] = False, 'no origin base branch to compare with'
        else:
            row['landed'], row['landed_by'] = False, 'recorded head commit is not in repository'
        if not row['landed']:
            reasons.append(f'not landed: {row["landed_by"]}')
    else:
        row['landed'] = False
    sid = owner.get('session') if owner else None
    owner_live = [s for s in live if sid and s['id'] == sid]
    if owner_live:
        s = owner_live[0]
        reasons.append(f'owner session {s["provider"]}:{sid} is ' + ('alive' if s['state'] == 'alive'
                                                                       else 'possibly alive (runtime unknown)'))
    row['in_use_by'] = []
    row['open_by'] = []
    reasons.extend(blockers)
    try:
        row['last_touched'] = datetime.fromtimestamp((ws_path / SIDECAR_FILE).stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
    except OSError:
        row['last_touched'] = None
    return {**row, 'action': 'keep' if reasons else 'remove', 'reasons': reasons}


def find_orphan_sidecars(repo, root, entries):
    repo_root = Path(root) / repo.name
    if not repo_root.is_dir():
        return []
    entry_paths = {Path(e['path']).resolve(strict=False) for e in entries}
    orphans = []
    try:
        children = sorted(repo_root.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.name.endswith('.ws') or not (child.is_dir() or child.is_symlink()):
            continue
        lane_p = child.with_name(child.name[:-3])
        if lane_p.resolve(strict=False) not in entry_paths and not lane_p.exists() and not lane_p.is_symlink():
            orphans.append(child)
    return orphans


def survey(workspace=None, root=None, repo_filter=None, base=None, providers=PROVIDERS,
           live=None, holders=None, pulls_for=None):
    pulls_for = pulls_for or gh_pulls
    workspace = Path(workspace).resolve() if workspace else workspace_default()
    root = Path(root).resolve() if root else root_default(workspace)
    repos, clones = classify(scan_repos(workspace, root))
    if repo_filter:
        repos = [r for r in repos if r.name == repo_filter or str(r) == str(Path(repo_filter).expanduser().resolve())]
        clones = [c for c in clones if Path(c['of']) in repos]
    blockers, errors = [], {}
    if live is None:
        live, errors = live_sessions(providers)
        blockers += [f'{p} session discovery failed' for p in errors]
    if holders is None:
        try:
            holders = open_files(skip={os.getpid()})
        except (WorktreeError, OSError, subprocess.SubprocessError) as error:
            holders, errors['lsof'] = [], str(error)
            blockers.append('open-file check (lsof) failed')
    rows = []
    for repo in repos:
        try:  # Repositories can move while the workspace is scanned; report, keep going.
            entries = worktrees(repo)
            orphans = find_orphan_sidecars(repo, root, entries)
            pulls = pulls_for(repo) if (len(entries) > 1 or orphans) else {}
            ref = base_ref(repo, base)
            rows.extend(assess(repo, entry, root, live, holders, pulls, ref, blockers) for entry in entries)
            rows.extend(assess_orphan_sidecar(repo, ws_p, root, live, pulls, ref, blockers) for ws_p in orphans)
        except (WorktreeError, OSError, subprocess.SubprocessError) as error:
            errors[f'repo {repo.name}'] = str(error)
    return {'workspace': str(workspace), 'root': str(root), 'worktrees': rows, 'clones': clones,
            'errors': errors, 'providers': list(providers)}


def remove(row):
    """Remove one assessed worktree (and its owned sidecar) or orphan sidecar after re-checking cleanliness."""
    if row.get('sidecar_only'):
        ws_path = Path(row['path'])
        lane_path = Path(row.get('worktree') or ws_path.with_name(ws_path.name[:-3]))
        if lane_path.exists() or lane_path.is_symlink():
            return {'removed': False, 'reason': 'worktree exists'}
        meta = read_sidecar(ws_path)
        if not valid_sidecar_meta(meta, lane_path, ws_path, row.get('repo')):
            return {'removed': False, 'reason': f'missing or invalid {SIDECAR_FILE}'}
        rmtree_sidecar(ws_path)
        branch = row.get('branch')
        deleted_branch = None
        if branch and row.get('repo'):
            try:
                repo = resolve_repo(row['repo'], ws_path.resolve(strict=False).parents[2])
                git(repo, 'worktree', 'prune', check=False)
                if git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0:
                    git(repo, 'branch', '-D', branch)
                    deleted_branch = branch
            except WorktreeError:
                pass
        return {'removed': True, 'branch_deleted': deleted_branch, 'sidecar_removed': True}
    path = Path(row['path'])
    owner = row.get('owner') if isinstance(row.get('owner'), dict) else read_owner(path)
    sidecar = sidecar_for(path, owner)
    if dirty_count(path) != 0:
        return {'removed': False, 'reason': 'became dirty'}
    if any(inside(name, path.resolve()) for _, _, name in open_files(skip={os.getpid()})):
        return {'removed': False, 'reason': 'a process opened it'}
    repo = main_checkout(path)
    if sidecar is not None:
        nm = path / 'node_modules'
        if nm.exists() or nm.is_symlink():
            rmtree_sidecar(nm)
    git(repo, 'worktree', 'remove', str(path))  # No --force: git refuses a dirty tree too.
    if sidecar is not None and (sidecar.exists() or sidecar.is_symlink()):
        rmtree_sidecar(sidecar)
    if row['branch']:
        git(repo, 'branch', '-D', row['branch'])  # -D: a squash-merged branch is never "merged".
    result = {'removed': True, 'branch_deleted': row['branch']}
    if sidecar is not None:
        result['sidecar_removed'] = True
    return result


def remove_owned(target_path, session=None, provider=None, workspace=None):
    """Owner cleanup path removing a clean worktree and its owned .ws sidecar (or absent-worktree sidecar)."""
    raw = Path(target_path).expanduser().resolve(strict=False)
    if raw.name.endswith('.ws'):
        ws_path = raw
        lane_path = raw.with_name(raw.name[:-3])
    else:
        lane_path = raw
        ws_path = sidecar_path(lane_path)
    if session is None:
        env_provider, env_session = calling_session()
        if provider in (None, env_provider):
            session = env_session
    if lane_path.exists() or lane_path.is_symlink():
        if not lane_path.is_dir():
            raise WorktreeError(f'{lane_path} is not a directory')
        repo = main_checkout(lane_path)
        if Path(out(lane_path, 'rev-parse', '--show-toplevel')).resolve() != lane_path.resolve():
            raise WorktreeError(f'{lane_path} is not the top of a worktree')
        if lane_path.resolve() == repo:
            raise WorktreeError(f'{lane_path} is the main checkout; refusing removal')
        owner = read_owner(lane_path)
        if owner is None:
            raise WorktreeError(f'{lane_path} has no {OWNER_FILE}; refusing to remove unowned worktree')
        if session and owner.get('session') and session != owner.get('session'):
            raise WorktreeError(f'{lane_path} is owned by session {owner.get("session")}, not {session}')
        if dirty_count(lane_path) != 0:
            raise WorktreeError(f'{lane_path} is dirty; refusing removal without --force')
        sidecar = sidecar_for(lane_path, owner)
        branch = git(lane_path, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False).stdout.strip() or owner.get('branch')
        if sidecar is not None:
            nm = lane_path / 'node_modules'
            if nm.exists() or nm.is_symlink():
                rmtree_sidecar(nm)
        git(repo, 'worktree', 'remove', str(lane_path))  # No --force: preserve clean worktree invariant.
        sidecar_removed = False
        if sidecar is not None and (sidecar.exists() or sidecar.is_symlink()):
            rmtree_sidecar(sidecar)
            sidecar_removed = True
        if branch and git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0:
            git(repo, 'branch', '-D', branch)
        return {'removed': True, 'path': str(lane_path), 'sidecar': str(sidecar) if sidecar else None,
                'sidecar_removed': sidecar_removed, 'branch_deleted': branch}
    if ws_path.exists() or ws_path.is_symlink():
        meta = read_sidecar(ws_path)
        if not valid_sidecar_meta(meta, lane_path, ws_path):
            raise WorktreeError(f'{ws_path} has no valid {SIDECAR_FILE} owner record; refusing to remove arbitrary .ws directory')
        owner = meta['owner']
        if session and owner.get('session') and session != owner.get('session'):
            raise WorktreeError(f'{ws_path} is owned by session {owner.get("session")}, not {session}')
        rmtree_sidecar(ws_path)
        branch = owner.get('branch')
        deleted_branch = None
        ws_space = Path(workspace).resolve() if workspace else lane_path.parents[2]
        try:
            repo = resolve_repo(meta.get('repo') or '', ws_space)
            git(repo, 'worktree', 'prune', check=False)
            if branch and git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0:
                git(repo, 'branch', '-D', branch)
                deleted_branch = branch
        except WorktreeError:
            pass
        return {'removed': True, 'path': str(lane_path), 'sidecar': str(ws_path),
                'sidecar_removed': True, 'branch_deleted': deleted_branch}
    raise WorktreeError(f'{target_path} does not exist')


def sweep(apply=False, **kwargs):
    report = survey(**kwargs)
    for row in report['worktrees']:
        if row['action'] == 'remove':
            row['result'] = remove(row) if apply else {'removed': False, 'reason': 'dry run'}
    linked = [r for r in report['worktrees'] if r['location'] != 'main']
    removable = [r for r in linked if r['action'] == 'remove']
    report['applied'] = apply
    report['summary'] = {'removable': len(removable), 'removed': sum(r['result']['removed'] for r in removable),
                         'keep': len(linked) - len(removable), 'clones': len(report['clones'])}
    return report


# --- Text output -------------------------------------------------------------

def text(report, sweep_mode=False):
    lines = [f'workspace {report["workspace"]}  root {report["root"]}']
    for key, value in report['errors'].items():
        lines.append(f'! {key}: {value}')
    for row in report['worktrees']:
        if row['location'] == 'main' and sweep_mode:
            continue
        owner = row.get('owner') or {}
        who = f'{owner.get("agent")}#{owner.get("issue")}' if owner else '-'
        pr = row.get('pr', {})
        pr_text = f'PR#{pr["number"]} {pr["state"]}' if pr.get('number') else pr.get('state', '-')
        dirty = row.get('dirty')
        state = '-' if dirty is None else ('clean' if dirty == 0 else f'dirty:{dirty}')
        users = len(row.get('in_use_by', [])) + len(row.get('open_by', []))
        lines.append(f'{row["action"]:6} {row["location"]:7} {row["path"]}  [{row["branch"] or "detached"}] '
                     f'{who} {pr_text} {state} in-use:{users} {row.get("last_touched") or ""}')
        shown = row['reasons'][1:] if row['location'] == 'main' else row['reasons']
        if shown:
            lines.append('       - ' + '; '.join(shown))
    for clone in report['clones']:
        lines.append(f'clone  {clone["path"]}  (same origin as {clone["of"]}; reported, never removed)')
    if 'summary' in report:
        lines.append(json.dumps(report['summary']))
    return '\n'.join(lines)


def main(argv=None, prog='wt'):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.split('\n\n')[0])
    places = argparse.ArgumentParser(add_help=False)
    places.add_argument('--workspace', help=f'Workspace holding the repositories (env {WORKSPACE_ENV}; '
                                            'default: parent of the current repository)')
    places.add_argument('--root', help=f'Worktree root (env {ROOT_ENV}; default: <workspace>/.worktrees)')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('new', parents=[places],
                       help='Create a worktree at <root>/<repo>/<agent>-<issue> and write its owner')
    p.add_argument('repo');p.add_argument('agent');p.add_argument('issue')
    p.add_argument('--base', default='main');p.add_argument('--detach', metavar='REF', help='Read-only pin, no branch')
    p.add_argument('--slug', help='Branch suffix (default: from the issue title via gh)')
    p.add_argument('--session', help='Owner session id (default: the calling session from the environment)')
    p.add_argument('--provider', choices=PROVIDERS)
    p.add_argument('--links-config', help=f'Workspace links manifest (env {LINKS_ENV}; default: <root>/{LINKS_FILE})')
    p.add_argument('--no-link', action='store_true', help='Skip .ws sidecar creation even when links config is present')
    p.add_argument('--peer', action='append', default=[], metavar='REPO=REF',
                   help='Override sibling git ref for .ws/peers/<REPO> snapshot materialization (repeatable)')
    p = sub.add_parser('adopt', help='Write the owner record of an existing worktree so sweep can judge it')
    p.add_argument('path');p.add_argument('--agent', required=True);p.add_argument('--issue', required=True)
    p.add_argument('--purpose', choices=['work', 'pin'], default='work')
    p.add_argument('--session');p.add_argument('--provider', choices=PROVIDERS)
    p.add_argument('--force', action='store_true', help='Replace an existing owner record')
    p = sub.add_parser('remove', parents=[places],
                       help='Remove an owned clean worktree and its .ws sidecar (or same-owner orphan sidecar) without --force')
    p.add_argument('path')
    p.add_argument('--session', help='Verify owner session id before removing (default: calling session from env)')
    p.add_argument('--provider', choices=PROVIDERS)
    for cmd in ('list', 'sweep'):
        p = sub.add_parser(cmd, parents=[places], help='List worktrees with owner, PR, state and live use' if cmd == 'list'
                           else 'Dry run of what would be removed; --apply removes')
        p.add_argument('--repo', help='Only this repository (name or path)')
        p.add_argument('--base', help='Base branch on origin (default: origin/HEAD, then main, master)')
        p.add_argument('--providers', default=','.join(PROVIDERS),
                       help='Session providers to consult; a failing one blocks removal (default: all)')
        p.add_argument('--text', action='store_true', help='One line per worktree instead of JSON')
        if cmd == 'sweep':
            p.add_argument('--apply', action='store_true', help='Remove the worktrees judged removable')
    args = parser.parse_args(argv)
    try:
        if args.cmd == 'new':
            result = new(args.repo, args.agent, args.issue, args.base, args.detach, args.slug, args.session,
                         args.provider, args.workspace, args.root, links_config=args.links_config,
                         no_link=args.no_link, peers=args.peer)
        elif args.cmd == 'adopt':
            result = adopt(args.path, args.agent, args.issue, args.purpose, args.session, args.provider, args.force)
        elif args.cmd == 'remove':
            result = remove_owned(args.path, session=args.session, provider=args.provider, workspace=args.workspace)
        else:
            providers = tuple(p for p in args.providers.split(',') if p)
            unknown = set(providers) - set(PROVIDERS)
            if unknown:
                raise WorktreeError(f'Unknown providers: {", ".join(sorted(unknown))}')
            kwargs = dict(workspace=args.workspace, root=args.root, repo_filter=args.repo, base=args.base,
                          providers=providers)
            result = sweep(args.apply, **kwargs) if args.cmd == 'sweep' else survey(**kwargs)
            if args.text:
                print(text(result, args.cmd == 'sweep'), flush=True)
                return 0
    except (WorktreeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
