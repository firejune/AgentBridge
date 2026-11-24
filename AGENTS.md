# Agent Bridge

Small local correspondence CLI; Python standard library only. See README.md for the protocol.

- Keep peer messages distinct from human authorization.
- Never automatically retry an ambiguous dispatch or auto-reply to acknowledgments.
- Keep live session IDs, socket files, and credentials out of Git.
- Delivery acceptance is not task completion. Bounded messages, delivery state, idempotency records, independent inbox snapshots and ACK receipts persist privately under the issue #94 correspondence contract. Event streams and provider diagnostics remain volatile.
- Run `python3 -m unittest discover -s tests -v` before committing changes.
- Only endpoints and channel bindings belong in the private routing snapshot (issue #5). Correspondence uses the separate bounded owner-private checkpoint (issue #94). Never persist credentials, signed handles/cursors, peer socket metadata or provider diagnostics. Never add automatic delivery retries.
- Narrow user-approved exception: opt-in MCP Events webhook signing keys may persist only in macOS Keychain, with bounded subscription metadata in a separate owner-private checkpoint. This does not permit credentials in routing, correspondence, Git, or diagnostic output, or persistence of other credentials.
- Narrow user-approved exception: bounded owner-private MCP receipt audit metadata may persist separately, limited to timestamps, fixed record/method/tool names, normalized stage/code/result fields, normalized public protocol versions and a broker-arrival correlated flag. Never include raw arguments, message bodies, callback URLs, keys or key references, live provider IDs, paths, sockets, or signed handles/cursors. Event streams and provider diagnostics otherwise remain volatile.
- Live CLI calls must target an explicitly registered session. Never pick the latest session.
- Narrow user-approved exception: `close --archive --provider agy --session UUID` may archive an Antigravity session that has no registered route. It is limited to archive, requires the exact UUID, and must refuse when any registered route or active channel binds that session, when the broker cannot be reached, or when the routing snapshot cannot be read. This does not permit any other live call on an unregistered session. (2026-11-24)
- Assign provider-specific internals to their provider's runner. Claude owns Claude Code and Desktop session, socket, and app internals. Codex/Sol owns Codex Desktop IPC. Antigravity owns Antigravity RPC. Cross-provider work may proceed only after finding relevant official public documentation, source code, or release notes from that provider. Otherwise, refer the work back to the provider's runner. This rule exempts provider-neutral broker code.
