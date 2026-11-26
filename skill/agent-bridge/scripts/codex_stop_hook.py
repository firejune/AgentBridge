#!/usr/bin/env python3
"""Deny one covered Codex tool call for a matching stop request or peer mail."""

import argparse
import json
import sys
import time

import broker
import codex_hook_mail

SOCKET_BUDGET = 0.25
MAX_INPUT = 4 * 1024 * 1024


def claim(socket, session, deadline):
    request = {'op': codex_hook_mail.CLAIM_OP, 'session': session, 'deadline': deadline}
    try:
        return broker.request(socket, request, deadline=deadline)
    except ValueError as error:
        if str(error) != 'Unknown operation':
            raise
        # An older broker still serves stop. Its old mail operations can consume the queue read.
        result = broker.request(socket, {**request, 'op': 'codex-stop-claim'}, deadline=deadline)
        return {**result, 'kind': 'stop'} if isinstance(result, dict) else None


def denial(reason):
    return json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
        'permissionDecisionReason': reason,
    }}) + '\n'


def emit_stop(result, socket, session, deadline, output):
    stamp, reason = result.get('requested_at'), result.get('reason')
    if (not isinstance(stamp, str) or not stamp.isprintable() or len(stamp) > 40
            or reason is not None and (not isinstance(reason, str)
                                       or not reason.isprintable() or len(reason) > 256)
            or time.monotonic() >= deadline):
        return
    message = f'Stopped by Agent Bridge at {stamp} by request of the local operator. End the turn now and wait.'
    if reason:
        message += f' Operator reason: {reason}'
    output.write(denial(message))
    output.flush()
    broker.request(socket, {'op': 'codex-stop-finish', 'session': session, 'id': result['id']},
                   deadline=deadline)


def run(socket, source, output):
    deadline = time.monotonic() + SOCKET_BUDGET
    try:
        raw = source.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get('hook_event_name') != 'PreToolUse':
            return
        session = event.get('session_id')
        if not codex_hook_mail.exact_uuid(session):
            return
        result = claim(socket, session, deadline)
        if not isinstance(result, dict) or not codex_hook_mail.exact_uuid(result.get('id')):
            return
        if result.get('kind') == 'stop':
            emit_stop(result, socket, session, deadline, output)
            return
        reason = result.get('reason')
        if (result.keys() != {'kind', 'id', 'reason'} or result['kind'] != 'mail'
                or not codex_hook_mail.valid_reason(reason) or time.monotonic() >= deadline):
            return
        payload = denial(reason)
        offer = broker.request(socket, {'op': codex_hook_mail.OFFER_OP, 'session': session, 'id': result['id'],
                                        'deadline': deadline, 'digest': codex_hook_mail.reason_digest(reason)},
                               deadline=deadline)
        if time.monotonic() >= deadline:
            return
        if offer == {'kind': 'mail', 'id': result['id']}:
            output.write(payload)
            output.flush()
        elif (isinstance(offer, dict) and offer.get('kind') == 'stop'
              and codex_hook_mail.exact_uuid(offer.get('id'))):
            emit_stop(offer, socket, session, deadline, output)
    except (OSError, ValueError, TypeError, RecursionError):
        # An offer can outlive a lost response or failed local write. The
        # ordinary queue read still has the body.
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default=str(broker.DEFAULT_SOCKET))
    args = parser.parse_args()
    run(args.socket, sys.stdin.buffer, sys.stdout)


if __name__ == '__main__':
    main()
