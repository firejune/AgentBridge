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
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sessions

OWNER_FILE = 'OWNER.json'
ROOT_ENV = 'AGENT_BRIDGE_WT_ROOT'
WORKSPACE_ENV = 'AGENT_BRIDGE_WORKSPACE'
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


def new(repo_spec, agent, issue, base='main', detach=None, slug=None, session=None, provider=None,
        workspace=None, root=None, slugger=issue_slug):
    agent, issue = check_name('AGENT', agent), check_name('ISSUE', issue)
    workspace = Path(workspace).resolve() if workspace else workspace_default()
    root = Path(root).resolve() if root else root_default(workspace)
    repo = resolve_repo(repo_spec, workspace)
    path = root / repo.name / f'{agent}-{issue}'
    if path.exists() or path.is_symlink():
        raise WorktreeError(f'{path} already exists; not reusing it')
    path.parent.mkdir(parents=True, exist_ok=True)
    if detach:
        git(repo, 'worktree', 'add', '--detach', str(path), detach)
        branch, start, purpose = None, detach, 'pin'
    else:
        start = f'origin/{base}' if git(repo, 'rev-parse', '--verify', '--quiet', f'origin/{base}^{{commit}}',
                                        check=False).returncode == 0 else base
        tail = slugify(slug) if slug is not None else slugger(repo, issue)
        branch = f'{agent}/{issue}' + (f'-{tail}' if tail else '')
        if git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0:
            raise WorktreeError(f'Branch {branch} already exists; not reusing it')
        git(repo, 'worktree', 'add', '--no-track', '-b', branch, str(path), start)
        purpose = 'work'
    record = write_owner(path, agent, issue, repo.name, purpose, session, provider,
                         {'branch': branch, 'base': start, 'head': out(path, 'rev-parse', 'HEAD')})
    return {'created': True, 'path': str(path), 'branch': branch, 'owner': record}


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
    record = write_owner(path, agent, issue, repo.name, purpose, session, provider,
                         {'branch': branch, 'adopted': True}, force=force)
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


def content_landed(path, base):
    """Every path the branch changed (merge-base to tip) is identical on base."""
    fork = git(path, 'merge-base', base, 'HEAD', check=False)
    if fork.returncode:
        return False, f'no merge-base with {base}'
    # -z: names arrive verbatim (no C-quoting of non-ASCII, spaces or quotes); :(literal) stops glob magic.
    names = git(path, 'diff', '-z', '--no-renames', '--name-only', fork.stdout.strip(), 'HEAD').stdout
    changed = [p for p in names.split('\0') if p]
    if not changed:
        return True, 'no changes beyond the merge-base'
    same = git(path, 'diff', '--quiet', '--no-renames', 'HEAD', base, '--',
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
            pulls = pulls_for(repo) if len(entries) > 1 else {}
            ref = base_ref(repo, base)
            rows.extend(assess(repo, entry, root, live, holders, pulls, ref, blockers) for entry in entries)
        except (WorktreeError, OSError, subprocess.SubprocessError) as error:
            errors[f'repo {repo.name}'] = str(error)
    return {'workspace': str(workspace), 'root': str(root), 'worktrees': rows, 'clones': clones,
            'errors': errors, 'providers': list(providers)}


def remove(row):
    """Remove one assessed worktree after re-checking that it is clean and unused."""
    path = Path(row['path'])
    if dirty_count(path) != 0:
        return {'removed': False, 'reason': 'became dirty'}
    if any(inside(name, path.resolve()) for _, _, name in open_files(skip={os.getpid()})):
        return {'removed': False, 'reason': 'a process opened it'}
    repo = main_checkout(path)
    git(repo, 'worktree', 'remove', str(path))  # No --force: git refuses a dirty tree too.
    if row['branch']:
        git(repo, 'branch', '-D', row['branch'])  # -D: a squash-merged branch is never "merged".
    return {'removed': True, 'branch_deleted': row['branch']}


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
    p = sub.add_parser('adopt', help='Write the owner record of an existing worktree so sweep can judge it')
    p.add_argument('path');p.add_argument('--agent', required=True);p.add_argument('--issue', required=True)
    p.add_argument('--purpose', choices=['work', 'pin'], default='work')
    p.add_argument('--session');p.add_argument('--provider', choices=PROVIDERS)
    p.add_argument('--force', action='store_true', help='Replace an existing owner record')
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
                         args.provider, args.workspace, args.root)
        elif args.cmd == 'adopt':
            result = adopt(args.path, args.agent, args.issue, args.purpose, args.session, args.provider, args.force)
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
