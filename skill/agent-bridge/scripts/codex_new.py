"""Open the app's new-thread composer and identify its exact Desktop owner.

The internal deep-link contract was read from the installed Desktop app. This
opens a composer; it never presses Enter or creates a standalone CLI thread.
"""
import math
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode

import codex_live

PROMPT = 'This session was opened by agent-bridge. Wait for a task from the user or agent-bridge.'


class CreationError(ValueError):
    def __init__(self, reason, uncertain=False, session=None):
        super().__init__(reason)
        self.uncertain = uncertain
        self.session = session


def new_session_url(cwd, prompt):
    return 'codex://threads/new?' + urlencode({'path':cwd, 'prompt':prompt, 'mode':'codex'})


def desktop_rows(cwd):
    # The app has no shared app-server socket on some installations. Saved
    # metadata is discovery only; the independent exact owner check below is
    # required to call the session live. No turn is started by this reader.
    import sessions
    listing = sessions.list_codex(cwd=cwd, limit=10000, mode='saved')
    if listing['more_available']:
        raise CreationError('Codex desktop session listing was truncated; not identifying a new session')
    return [row for row in listing['sessions'] if interactive_row(row, Path(cwd))]


def interactive_row(row, folder):
    # SourceKind is owned by app-server and may change. A root source string
    # is only a candidate; the exact Desktop owner is the decisive check.
    source = row.get('source')
    return (isinstance(source, str) and source != 'exec'
            and isinstance(row.get('cwd'), str) and Path(row['cwd']).resolve() == folder)


def open_link(url):
    import shutil
    import subprocess
    if not shutil.which('open'):
        raise CreationError('Opening a Codex desktop session needs the macOS `open` command')
    result = subprocess.run(['open', url], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=10)
    if result.returncode:
        raise CreationError('The codex:// new-thread link was not opened')


def open_codex(cwd, title=None, model=None, timeout=180, opener=None, listing=None,
               owner_factory=codex_live.Client, poll=0.5):
    if model is not None:
        raise CreationError('The Codex desktop new-thread link carries no model; choose it in the composer instead of --model')
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise CreationError('Timeout must be between 0 and 3600 seconds')
    if not cwd or not str(cwd).strip():
        raise CreationError('Workspace --cwd is required to open a Codex desktop session')
    folder = Path(cwd).expanduser().resolve()
    if not folder.is_dir():
        raise CreationError(f'Workspace directory does not exist: {folder}')
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise CreationError('--title must be a nonempty first message')
    prompt = title.strip() if title is not None else PROMPT
    read = listing or desktop_rows
    before = {row['id'] for row in read(str(folder))}
    url = new_session_url(str(folder), prompt)
    opened, sid = False, None
    try:
        # A partial/ambiguous launcher outcome must not trigger a second link.
        opened = True
        (opener or open_link)(url)
        deadline = time.monotonic() + timeout
        while True:
            rows = [row for row in read(str(folder)) if row['id'] not in before
                    and interactive_row(row, folder)]
            ids = {row['id'] for row in rows}
            if len(ids) > 1:
                raise CreationError('Multiple new Codex sessions appeared in --cwd; not choosing one. Bind the exact session with register --discover --session ID.')
            if ids:
                sid = next(iter(ids))
                try:
                    with owner_factory(timeout=min(max(deadline-time.monotonic(), 0.01), 2)) as client:
                        owner = client.owner(sid)
                except (codex_live.DeliveryError, OSError):
                    owner = None  # A saved row can precede the Desktop owner.
                if owner:
                    row = next(row for row in rows if row['id'] == sid)
                    return {'created':True, 'session':row, 'owner':owner, 'url':url,
                            'prompt':prompt, 'warning':None}
            if time.monotonic() >= deadline:
                raise CreationError('No new owned Codex desktop session appeared in --cwd before timeout. The composer is prefilled; press Enter in the app, then bind the exact session with register --discover --session ID.')
            time.sleep(min(poll, max(0, deadline-time.monotonic())))
    except CreationError as error:
        error.uncertain = opened
        error.session = sid
        raise
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        raise CreationError('Codex desktop opening or discovery failed; do not reopen automatically',
                            uncertain=opened, session=sid) from None
