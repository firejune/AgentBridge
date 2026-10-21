# Agent Bridge

Small local correspondence CLI; Python standard library only. See README.md for the protocol.

- Keep peer messages distinct from human authorization.
- Never automatically retry an ambiguous dispatch or auto-reply to acknowledgments.
- Keep live session IDs, socket files, and credentials out of Git.
- Delivery acceptance is not task completion. Messages, delivery state, idempotency keys, and event history live only in broker memory.
- Run `python3 -m unittest discover -s tests -v` before committing changes.
- Only endpoints and channel bindings may persist in the private routing snapshot (issue #5). Do not persist messages, credentials, peer socket metadata, delivery state, or events; never add automatic delivery retries.
- Live CLI calls must target an explicitly registered session. Never pick the latest session.
