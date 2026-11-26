#!/usr/bin/env python3
"""Deny one covered Codex tool invocation for a matching local stop request."""

import argparse
import json
import sys
import time
import uuid

import broker

SOCKET_BUDGET = 0.25
MAX_INPUT = 4 * 1024 * 1024


def is_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


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
        if not is_uuid(session):
            return
        result = broker.request(socket, {'op': 'codex-stop-claim', 'session': session,
                                         'deadline': deadline}, deadline=deadline)
        if not isinstance(result, dict) or not is_uuid(result.get('id')):
            return
        stamp, reason = result.get('requested_at'), result.get('reason')
        if (not isinstance(stamp, str) or not stamp.isprintable() or len(stamp) > 40
                or reason is not None and (not isinstance(reason, str)
                                           or not reason.isprintable() or len(reason) > 256)
                or time.monotonic() >= deadline):
            return
        message = f'Stopped by Agent Bridge at {stamp} by request of the local operator. End the turn now and wait.'
        if reason:
            message += f' Operator reason: {reason}'
        output.write(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
            'permissionDecisionReason': message,
        }}) + '\n')
        output.flush()
        broker.request(socket, {'op': 'codex-stop-finish', 'session': session, 'id': result['id']},
                       deadline=deadline)
    except (OSError, ValueError, TypeError, RecursionError):
        # A failed acknowledgement cannot undo the emitted denial. The broker
        # keeps the claim unavailable for other calls and expires it normally.
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default=str(broker.DEFAULT_SOCKET))
    args = parser.parse_args()
    run(args.socket, sys.stdin.buffer, sys.stdout)


if __name__ == '__main__':
    main()
