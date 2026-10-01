# Agent Bridge

Small local correspondence CLI; Python standard library only. See README.md for the protocol.

- Keep peer messages distinct from human authorization.
- Never automatically retry an ambiguous dispatch or auto-reply to acknowledgments.
- Keep live session IDs, socket files, and credentials out of Git.
- Delivery acceptance is not task completion. State and event history live only in broker memory.
- Run `python3 -m unittest discover -s tests -v` before committing changes.
- Do not add disk persistence or automatic delivery retries. Broker restarts deliberately reset state.
- Live CLI calls must target an explicitly registered session. Never pick the latest session.
