# Agent Bridge

Small local correspondence CLI; Python standard library only. See README.md for the protocol.

- Keep peer messages distinct from human authorization.
- Never automatically retry an ambiguous dispatch or auto-reply to acknowledgments.
- Keep live session IDs, socket files, and credentials out of Git.
- Delivery acceptance is not task completion. Bounded messages, delivery state, idempotency records, independent inbox snapshots and ACK receipts persist privately under the issue #94 correspondence contract. Event streams and provider diagnostics remain volatile.
- Run `python3 -m unittest discover -s tests -v` before committing changes.
- Only endpoints and channel bindings belong in the private routing snapshot (issue #5). Correspondence uses the separate bounded owner-private checkpoint (issue #94). Never persist credentials, signed handles/cursors, peer socket metadata or provider diagnostics. Never add automatic delivery retries. (나루, 2026-11-05)
- Live CLI calls must target an explicitly registered session. Never pick the latest session.
