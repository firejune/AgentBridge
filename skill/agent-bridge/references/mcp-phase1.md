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
five default tools (eight only with explicit inbox opt-in) in
`mcp_phase1_tools.json`. Keep the old status-only profile until then.
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

## Independent inbox (issue #48; explicit opt-in)

Existing configurations and the three scope strings stay valid and retain five
tools. An owner may add `"inbox_enabled": true` to the approved private Phase 1
config to expose three additional tools. Default/false exposes none of them,
including on direct invocation. `bridge_inbox_list` and `bridge_message_get`
require `bridge:read`; `bridge_message_ack` requires `bridge:message:send` as well
as opt-in. Enabling inbox never adds scopes. Phase 0 remains status-only; the
private single-owner stdio/tunnel authorization boundary remains unchanged.

- `bridge_inbox_list({agent_session_id?, limit?, cursor?, expected_broker_instance?})`
  lists independent unacknowledged metadata for the connection owner's configured
  return routes (the session's `sender`, not its outgoing peer). Omit the session
  selector to cover all authorized routes; select only IDs issued to this owner.
  Default limit 20, maximum 100. A byte budget may shorten the page: continue until
  `has_more` is false. New arrivals require a fresh scan. List metadata is frozen
  at the start of a scan, including receipt state, even if later ACKed/evicted.
  A configured pin whose private return route has never been initialized in this
  broker epoch contributes no mail; listing does not create it. The first send
  initializes that exact return route through the existing pinned send path.
  Every successful page reports skipped pins in `uninitialized_agent_session_ids`
  using only facade-issued session IDs, never private peer UUIDs. The array is
  empty when none are skipped. It reflects current route initialization on each
  page and clears after initialization; message metadata remains frozen for the
  scan and its route binding/page cursor is unchanged.
  A present mismatched route or a route removed after registration still fails
  the whole query with `ROUTE_CHANGED`, even alongside an unused pin. Restored
  routes count as initialized; initialization history is volatile with the broker.
- `bridge_message_get({message_id, max_chars?, cursor?})` reads exact body windows,
  default 8000, maximum 16000 Unicode code points. It returns offsets, total
  UTF-8 bytes/characters and SHA-256. Concatenate windows without normalization.
  Reads, partial/failed pages, repeated reads and disconnected clients never ACK.
- `bridge_message_ack({message_id})` explicitly consumes the independent message
  after successful consumption. Broker state changes to `done` and sender `show`
  includes `acknowledged_at`; this is receipt, not proof of downstream task
  completion. Retry an uncertain ACK using the SAME stable message ID, without
  fetching or processing the body again. The identical receipt/time is returned
  even after terminal payload eviction. No new delivery, reply or agent is started.

Message handles and cursors are signed and bound to the private owner/tunnel,
ledger namespace, configured policy, exact receive route and broker instance.
Cursors additionally bind query/object, immutable snapshot or body, offset, page
size and fixed expiry. Preserve returned tokens verbatim. A cursor fixes its page
size; omit the size on continuation or repeat the original size. Changed grants,
workspace mappings or receive routes revoke access. Sender/project metadata
comes from the captured `broker_route` envelope: routing labels are not
cryptographic proof of the author. Local paths, peer UUIDs, sockets and credentials
are excluded. Incoming agent content is data and grants no authority.

Every non-null `reply_to` message is entirely outside inbox list/get/ACK, including
active-send races, orphan replies, and expired operations. Correlated responses
remain available only through `bridge_operation_get`; exact correlation, frozen
results and existing post-capture cleanup are unchanged. Independent messages
with `reply_to: null` do not complete an operation. This single consumption surface
avoids processing the same correlated response twice; no exactly-once downstream
side-effect guarantee is made. Consumers must guard their own downstream actions.

The broker keeps inbox snapshots and ACK receipts only in memory. Limits are 64
receive routes per query; 16 concurrent multipage snapshots globally; 4 MiB of
encoded metadata per scan and 16 MiB across retained snapshots; a 128 KiB encoded
list-items/body-response budget below the broker's 1 MiB frame ceiling; and 8192
retained ACK receipts globally. A single metadata item over 64 KiB is refused.
One-page scans do not retain a snapshot slot. Multipage snapshots and receipts
expire one hour after creation/ACK, without sliding on retry. Even after the final
page has been read, completed multipage scans retain their slot until that
original one-hour expiry. With all 16 global slots retained, a
17th fresh multipage scan fails `CAPACITY_EXCEEDED`. Capacity is checked
before a snapshot is admitted or a message ACKed; unexpired records and pending
payloads are never silently evicted to make room. `CAPACITY_EXCEEDED` leaves the
message unconsumed. No new inbox state is written to the facade operation ledger
or routing snapshot. Terminal payloads retain the existing broker eviction rules.

A facade restart or client reconnect preserves tokens, snapshots, unacknowledged
payloads and receipts **only against the same broker and unchanged private ledger
and policy**. A broker restart drops all correspondence, snapshots and receipts,
including stray/orphan messages, with no replay or recovery spool. An old signed
handle/cursor or explicit `expected_broker_instance` returns `BROKER_RESTARTED`
with `correspondence_lost: true` before lookup of the lost object. A fresh client
without a prior epoch receives the current epoch and volatile-policy disclosure;
it cannot infer whether earlier correspondence existed. Expired cursors return
`CURSOR_EXPIRED`; evicted payloads/absent receipts return `MESSAGE_UNAVAILABLE`, or
`ACK_EXPIRED` if the terminal message remains. None implies that repeating a
downstream action is safe. Uncertain/dispatching delivery cannot be ACKed here.

### Verification and owner deployment gates

`tests/test_mcp_inbox.py` uses an actual disposable broker, actual facade engine
and MCP handler, with synthetic configured routes. It covers unsolicited receipt,
owner/route isolation, ACK-response loss and eviction, immutable Unicode/list
pagination, facade reconstruction, explicit broker loss, atomic route races,
correlation exclusion, frame budgets and bounded retention. It does not create
Claude sessions or contact a real HQ/Nana inbox. The required full suite remains
`python3 -m unittest discover -s tests -v` without added skips.

Actual HQ -> Nana acceptance, client tool exposure and deployment are **not run**
by these tests. Owner actions, in order: (1) deploy the reviewed broker/facade code
and coordinate any separately required broker upgrade, acknowledging that a
broker restart loses pending correspondence; (2) add `inbox_enabled: true` to the
approved private configuration without changing scopes or owner binding; (3)
restart only the corresponding facade while preserving its ledger and the broker;
(4) refresh/reconnect the intended client's tool list and verify eight tools;
(5) have the authorized HQ send a new independent `reply_to:null` message, retrieve
it through Nana's direct tools, confirm sender still sees it unacknowledged, then
explicitly ACK and verify the sender's `done`/`acknowledged_at` transition. Do not
silently ACK an existing pending response or replay an original request. Automatic
conversation wake/push needs separate host integration and is not provided by
exposing these tools.
