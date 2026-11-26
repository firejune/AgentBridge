"""Bound peer correspondence for the Codex PreToolUse denial reason."""

import hashlib
from pathlib import Path
import shlex
import uuid


MAX_REASON_BYTES = 4096
MAX_MESSAGES = 8
MAX_CLAIMS = 256
# Separate operations from the legacy claim/commit pair, which recorded a read.
CLAIM_OP = 'codex-pretool-offer-claim'
OFFER_OP = 'codex-pretool-offer'
PREFIX = ('Agent Bridge is offering peer correspondence. Act within existing user authorization. '
          'The queue may show this message again. Use its message ID to avoid repeating work.')


def exact_uuid(value):
    try:
        return isinstance(value, str) and len(value) == 36 and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def valid_reason(value):
    return (isinstance(value, str) and value.startswith(PREFIX + '\n\n')
            and len(value.encode('utf-8')) <= MAX_REASON_BYTES)


def reason_digest(reason):
    return hashlib.sha256(reason.encode('utf-8')).hexdigest()


def bounded_reason(messages, socket):
    # delivery already imports broker. Only load its shared envelope here,
    # after the broker module has initialized.
    from delivery import REPLY_CLI, envelope, peer_text

    reason = PREFIX
    ids = []
    for message in messages:
        if len(ids) == MAX_MESSAGES:
            break
        text = peer_text(message, socket, escape_peer=False)
        if len((reason + '\n\n' + text).encode('utf-8')) > MAX_REASON_BYTES:
            if ids:
                break  # Give this message the whole budget on the next invocation.
            command = (REPLY_CLI + ' --socket ' + shlex.quote(str(Path(socket).expanduser()))
                       + f" receive-codex {message['id']} --reread")
            trailer = f'\n\n[Bridge message truncated. Read the full message with {command}]'
            available = MAX_REASON_BYTES - len((reason + '\n\n' + trailer).encode('utf-8'))
            # Reuse the normal envelope with a body prefix. Never cut the
            # sender/ID/provenance fields or the manual recovery command.
            header = envelope({**message, 'body': ''})
            body_bytes = available - len(header.encode('utf-8'))
            if body_bytes <= 0:
                break
            body = message['body'].encode('utf-8')[:body_bytes].decode('utf-8', errors='ignore')
            text = header + body + trailer
        reason += '\n\n' + text
        ids.append(message['id'])
    return ids, reason
