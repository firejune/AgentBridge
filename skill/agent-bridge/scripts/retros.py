"""Owner-private, append-only project retrospectives; no provider internals."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import uuid


def store_path(state_dir, project, override=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project) or project in ('.', '..'):
        raise ValueError('RETRO_PROJECT_INVALID: use a single project name, not a path')
    return Path(override or os.environ.get('AGENT_BRIDGE_RETRO_STORE') or
                Path(state_dir) / 'retros' / f'{project}.jsonl').expanduser()


def notify_route(state_dir, project, explicit=None):
    if explicit is not None:
        if not explicit.strip():
            raise ValueError('RETRO_NOTIFY_INVALID: route must not be empty')
        return explicit
    path = Path(state_dir) / 'policy.json'
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('RETRO_NOTIFY_INVALID: policy must hold an object')
    routes = config.get('retro_notify', {})
    if not isinstance(routes, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                               for k, v in routes.items()):
        raise ValueError('RETRO_NOTIFY_INVALID: policy retro_notify must map projects to route names')
    return routes.get(project)


def record(project, card, runner, model, session, text=None, reason=None, tower=None,
           urgency='note', tags=''):
    if (text is None) == (reason is None):
        raise ValueError('RETRO_REQUIRED: provide a retro file or --no-retro reason')
    if text is not None and not text.strip():
        raise ValueError('RETRO_EMPTY: retro file must contain text')
    if reason is not None and not reason.strip():
        raise ValueError('RETRO_REASON_EMPTY: --no-retro needs a reason')
    return dict(id=str(uuid.uuid4()), time=datetime.now(timezone.utc).isoformat(),
                project=project, tower=tower, card=card, runner=runner, model=model,
                session=session, urgency=urgency, tags=list(dict.fromkeys(t.strip() for t in tags.split(',') if t.strip())),
                text=text, no_retro=reason)


def notification_body(row, target):
    if not target or row['urgency'] not in ('stop', 'repeat'):
        return None
    excerpt = '\n'.join((row['text'] or row['no_retro']).splitlines()[:5])[:1000]
    return (f"Retro {row['urgency']}: card={row['card']} runner={row['runner']} "
            f"tags={','.join(row['tags'])}\n{excerpt}")


def append(path, row, *, once=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8')
    access = os.O_RDWR if once else os.O_WRONLY
    fd = os.open(path, access | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        if once:
            with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as source:
                for line in source:
                    if line.strip() and json.loads(line)['id'] == row['id']:
                        return
        view = memoryview(data)
        while view:
            size = os.write(fd, view)
            if size <= 0:
                raise OSError('RETRO_WRITE_FAILED')
            view = view[size:]
        os.fsync(fd)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(fd)


def rows(path, project, since=None, tag=None, urgency=None):
    cutoff = None
    if since:
        cutoff = datetime.fromisoformat(since.replace('Z', '+00:00'))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    if not path.exists():
        return []
    with path.open(encoding='utf-8') as source:
        fcntl.flock(source, fcntl.LOCK_SH)
        found = [json.loads(line) for line in source if line.strip()]
    return [r for r in found if r['project'] == project
            and (cutoff is None or datetime.fromisoformat(r['time']) >= cutoff)
            and (tag is None or tag in r['tags']) and (urgency is None or urgency == r['urgency'])]


def tally(found):
    result = {}
    for row in found:
        for tag in row['tags']:
            entry = result.setdefault(tag, dict(tag=tag, count=0, first=row['time'], last=row['time'], cards=[]))
            entry['count'] += 1
            entry['first'] = min(entry['first'], row['time'])
            entry['last'] = max(entry['last'], row['time'])
            if row['card'] not in entry['cards']:
                entry['cards'].append(row['card'])
    return [result[tag] for tag in sorted(result)]
