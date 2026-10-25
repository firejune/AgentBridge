# MCP Phase 1 (contract 0.1.0)

The default remains `connectivity_only`: only `bridge_status` is loaded and callable.
The existing tunnel profile is not changed by installing this code. Phase 1 must
be explicitly authorized and enabled using `--phase1-config /absolute/private.json`.
The opt-in process is **stdio only**, behind a verified private single-owner tunnel.
`--http` plus Phase 1 is refused: loopback alone is not authentication. No API key,
principal, workspace path, model, route, or existing Claude UUID is a tool argument.

## Activation boundary

Before launching the opted-in process, verify the tunnel/plugin's allowed user,
and approve the workspace paths and create/send scopes. Config is an owner-only
0600 regular JSON file. Its fields are:

```json
{
  "principal": "operator-assigned-private-owner",
  "tunnel_id": "verified-private-tunnel-id",
  "writes_enabled": true,
  "scopes": ["bridge:read", "bridge:session:create", "bridge:message:send"],
  "state_dir": "/APPROVED/PRIVATE/MCP-STATE",
  "workspaces": {
    "approved-alias": {
      "cwd": "/APPROVED/WORKSPACE",
      "root": "/APPROVED/WORKSPACE",
      "project": "dedicated-mcp-project",
      "ide": "antigravity-ide"
    }
  }
}
```

This is a template, not a deployed configuration. Principal/tunnel binding is an
operator attestation to the verified single-owner stdio transport, **not a claim
that client-supplied identity or HTTP headers have been authenticated**. Do not
share the plugin or pipe with another principal. Multi-user authentication is not
implemented. The ledger pins the principal+tunnel binding and refuses reuse under
another binding. Workspaces must already exist, be canonical absolute paths,
fall within their root, and retain their accepted mapping. Approve any alias
changes separately. This config does not change the receiving agent's permissions.

Reconnect/re-discover the plugin after an approved activation; it will list the
five tools in `mcp_phase1_tools.json`. Keep the old status-only profile until then.
No launch agent, key rotation, public listener, model override, or shared plugin is
installed by this implementation.

## Durable queue and delivery

`mcp_phase1.py` owns a private 0700 state directory and 0600 SQLite ledger; the
broker still keeps only routes durably and correspondence in memory. This
**separate, explicitly requested facade ledger** stores owned sessions, durable
operation/key hashes, exact accepted payloads and fixed correlated replies.
It is not a modification to the broker's persistence policy. Treat it as private
conversation data; do not put it in Git or include it in diagnostic bundles.

An immediate transaction reserves an operation and session busy slot before
returning accepted. Same principal/tool/key+payload returns the original IDs/time;
changed payload conflicts. A single worker per ledger is enforced with an OS
lock. All facade ledgers for this OS user share a host creation lock; independent
human/CLI IDE operations can still race, in which case the native creation
adapter refuses ambiguity. Create and send are separate operations.

The worker commits intent before create/post/push, saves returned UUID/message
IDs before the next stage, and actually calls `bridge.push_if_peer` after broker
`send`/`State.post`. It uses dedicated random routes, verifies exact UUID/cwd,
and never chooses an existing session or channel. New Claude sessions use the
supported IDE opening path and current host model under the existing model policy.
Only metadata of adapter-owned sessions is exposed. Unknown backend availability
is `unavailable`, never guessed ready. Delivery acceptance is not completion.

Only exact `reply_to` responses form a success; the first observed nonempty bundle
is sorted by `(created,id)` and frozen. Pagination slices Unicode code points.
Cursors are signed and bound to owner, query kind, object, offset and one-hour
expiry. Completed output is untrusted agent data.

Limits: 64 active/unknown operations, 64 MiB ledger admission threshold, 1 MiB
fixed response bundle, 32768 UTF-8 bytes per input, 5000 ms lookup wait, and 16000
characters per page. Read-only lookups do not wait on the backend; the worker does.
SQLite failures prevent new effects. Backpressure never discards unresolved work.
Full terminal details are retained for 720 hours **after completion**, exceeding
both specified minima. Subsequent maintenance removes payload/result details but
retains operation/key/hash tombstones for the ledger lifetime. Unknown/in-flight
records are never aged out. Detail expiry cannot recycle a key.

## Restart and manual recovery

Queued operations have no external intent and may resume. Anything with a
persisted intent is never blindly replayed. For a known message on the same broker
instance, recovery observes only; missing IDs, eviction, broker restart, pending
or dispatching ambiguity become `unknown`. A reply body can settle a known message.
Unknown message operations block further sends to that owned session. No tool
resolves uncertainty, deletes a ledger, cancels work, or sends with a fresh key.
Exactly-once external effects are not promised.

If creation yielded a known UUID but registration did not finish, an operator may
explicitly repair registration of **that exact UUID only**. After stopping the
corresponding opted-in facade by a separately approved operational action:

```
python3 mcp_server.py --socket /APPROVED/BROKER.sock \
  --phase1-config /APPROVED/private.json --recover-registration op_KNOWN_OPERATION
```

The exclusive ledger lock prevents concurrent repair; this command never opens a
session or sends a message. It verifies identity/workspace and refuses conflicting
routes. If the UUID was never obtained, inspect manually; there is no automatic
create retry. An interrupted worker preserves its ledger and known partial IDs.

## Verification

`python3 -m unittest discover -s tests -v` includes opt-in and default-off transport,
input/schema, ownership, concurrent key/replay, busy, immutable pagination,
permissions, async wait, restart/eviction/crash, disk failure, and known-UUID repair
tests. The integration test uses a real temporary broker while mocking IDE open,
peer resolution and the external push boundary. Existing peer-protocol tests cover
fake Unix sockets. None is evidence of live Claude creation or a live write tool
being authorized/exposed. The live status-only proof is separate.

## Explicit pinned peer grants

An operator may configure optional `pinned_sessions`, keyed by a distinct alias.
Each grant requires canonical `uuid`, existing canonical `cwd`, `project`,
existing route name `route`, and ISO timestamp `authorized_at`. This is an
explicit external-session grant, not adapter ownership. It cannot be used with
session_create and does not authorize other sessions in that directory.
The existing five tool schemas remain unchanged: list returns an opaque as_ ID,
send accepts that ID, and operation_get retrieves only replies to that request.

Every delivery verifies UUID/cwd and the existing claude-peer route. Missing or
retargeted routes fail closed; only a dedicated realtime reply route may be
registered. Busy pinned peers accept live messages without interrupting their
turn. Dedicated created sessions retain their existing ready-only admission.
Changing or removing a grant invalidates its ID and prevents queued dispatch;
already sent requests may still finish. No transcript or historical HQ messages
are exposed. Reply content remains untrusted. Local grants and live IDs must
never be committed to source control.

## Broker reply retention and downstream keys

New operations persist a broker key derived from a durable random ledger namespace
and the existing client-key hash. Existing operations without this field retain
their original downstream key; client replay hashes and uncertain-send rules are
unchanged. Namespace migration does not redispatch anything.

After correlated reply text is committed, the facade marks those replies done
through the existing broker resolve API. Pending cleanup IDs are durable and
retried without resending; cleanup failures never overwrite a successful result.
Cleanup stops if the broker instance changes and never resolves uncertain replies.
Previously completed operations without cleanup IDs are not retroactively swept.
