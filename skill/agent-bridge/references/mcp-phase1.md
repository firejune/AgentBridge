# MCP Phase 1 (contract 0.1.0)

Local agents (Claude Code, Codex, Antigravity) invoke `bridge.py` directly via the Python CLI and do not register a local MCP server. `mcp_server.py` is dedicated to a single remote connector behind a verified private single-owner tunnel, enabled via `--phase1-config /absolute/private.json` over **stdio only**. (If invoked without `--phase1-config`, the server falls back to `connectivity_only`, exposing only `bridge_status`; `--http` plus Phase 1 is refused because loopback alone is not authentication.) No API key, principal, workspace path, model, route, or existing session UUID is a tool argument.

## Activation boundary

Before launching the tunnel process, verify the tunnel/plugin's allowed user,
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

The facade reads this file once, at start. A change to `workspaces`,
`pinned_sessions`, `inbox_enabled`, `events_enabled` or `mcp_2026_enabled` takes effect only after the facade process
is restarted; the broker does not need a restart. Until then a live session in a
newly added workspace is not listed by `bridge_sessions_list`. Before restarting,
check that no operation in the ledger is `accepted` or `running`. Inbox handles
and cursors are signed over the workspace and pinned-session policy, so those
issued before the change are refused afterwards (`INVALID_ARGUMENT`); list the
inbox again.

Register only a single remote connector (`Agent Bridge`) pointing to this tunnel; do not maintain a duplicate status-only connector or local MCP entry. It exposes the five default tools (eight when `inbox_enabled: true` is set) defined in `mcp_phase1_tools.json`.
No launch agent, key rotation, public listener, model override, or shared plugin is
installed by this implementation.

## Durable queue and delivery

`mcp_phase1.py` owns a private 0700 state directory and 0600 SQLite ledger; the
broker separately checkpoints routing and bounded correspondence. This
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
and never chooses an arbitrary workspace outside the approved config.
`bridge_session_create` accepts an optional `agent_type` (`"claude"`, `"agy"`,
or `"codex"`, defaulting to the workspace's configured `agent_type` or `"claude"`)
and opens new sessions through the corresponding provider adapter (`claude-peer`,
`agy-live`, or `codex-live`). `bridge_sessions_list` exposes owned sessions,
pinned sessions, and live `claude`/`agy`/`codex` sessions discovered within the
approved workspace roots (`cwd`/`root`) using opaque `as_` IDs (never raw
UUIDs or paths), along with each session's human-readable `title`, broker
`route_name`, nullable `created_at` / `last_activity_at` (null when unknown
rather than substituted with poll time), and `observed_at`. `bridge_status`
includes `runtime_revision`, `tool_schema_revision`, and `supported_agent_types`
to distinguish server contract updates from live provider availability or
client-cached tool catalogs. Unknown backend availability is `unavailable`, never guessed
ready. Delivery acceptance is not completion.

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
Unknown message operations block further sends to that owned session.

Reading a known message is not a delivery outcome (issue #134). While an operation
is `running`, a failed read (broker timeout, disconnect, unreadable reply) leaves
it `running`; the next read waits 1 s, doubling per consecutive error, and the
sixth consecutive error closes the operation as `unknown`. Backend `failed` /
`uncertain` and a broker instance change still close it at once. The last read
error (class, message, time) and the consecutive count are kept on the ledger
record as `error` / `observe_errors`; they are never part of a tool result.

A closed `unknown` or `failed` message operation with a known message is still
read on the same broker instance: every 5 s for 10 minutes after it closed, then
every 60 s. A correlated reply settles it as `succeeded` (the previous status and
code stay on the ledger as `settled_after`), resolves the reply, and frees the
session. The caller reads it with `bridge_operation_get` on the same
`operation_id`. This watch only reads: it never posts, pushes or resends, and it
ends on a broker instance change, when the broker no longer knows the message, or
when the record expires. Correlated replies stay out of the independent inbox. No tool
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

The broker checkpoints inbox snapshots, unacknowledged payloads and ACK receipts
in its private correspondence store. Limits are 64
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

A facade restart or client reconnect preserves tokens against the same broker
and unchanged private ledger and policy. A broker restart preserves bounded
correspondence and independent unread payloads. Epoch-bound snapshots and ACK
receipts are validated and discarded on broker startup, releasing their capacity.
It never replays a delivery. Existing signed handles/cursors and explicit old
`expected_broker_instance` still return `BROKER_RESTARTED`. A fresh inbox list
issues new epoch-bound handles for the surviving unread mail. Use those handles
for get and ACK. The same newly issued handle returns the identical ACK receipt
on retry through its original deadline, including after terminal payload eviction.
A previously ACKed message stays consumed across restart. Old epoch handles stay
revoked. The consumed message state and acknowledgement time survive. Reconciliation using an old handle
across broker epochs is unavailable. A reader with an uncertain pre-restart ACK
must reconcile its downstream actions before deciding to process any surviving
mail. No exactly-once side-effect guarantee is added.

Instance pings share the bounded 3-second RPC budget with other facade calls.
A slow checkpoint taking over 0.5 seconds can complete before the ping is treated
as backend loss. An unresponsive connected broker is detected within 3 seconds
rather than the former 0.5-second budget. A refused connection fails immediately.
This budget does not reduce the cost of whole-checkpoint mutations.

`BROKER_RESTARTED.correspondence_lost` reflects the broker's durable loss ledger.
It is false for an intact checkpoint, true after recorded corruption or when the
broker cannot confirm recovery. Fresh lists disclose the durable restart policy.
See [correspondence storage](usage.md#correspondence-storage-issue-94) for the exact
field allowlist, byte ceiling, atomic commit and durable dropped-ID diagnostics.
Expired cursors return `CURSOR_EXPIRED`. Evicted payloads/absent receipts return
`MESSAGE_UNAVAILABLE`, or `ACK_EXPIRED` if the terminal message remains.
Uncertain/dispatching delivery cannot be ACKed here.

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
and coordinate any separately required broker upgrade, preserving the private
correspondence checkpoint and reconciling uncertain pre-restart ACKs; (2) add `inbox_enabled: true` to the
approved private configuration without changing scopes or owner binding; (3)
restart only the corresponding facade while preserving its ledger and the broker;
(4) refresh/reconnect the intended client's tool list and verify eight tools;
(5) have the authorized HQ send a new independent `reply_to:null` message, retrieve
it through Nana's direct tools, confirm sender still sees it unacknowledged, then
explicitly ACK and verify the sender's `done`/`acknowledged_at` transition. Do not
silently ACK an existing pending response or replay an original request. Automatic
conversation wake/push is not provided by exposing these tools alone; the
optional Events integration below supplies a separate notification path.

## Optional MCP 2026 protocol

`"mcp_2026_enabled": true` in the private Phase 1 config enables dual-era
stdio handling. It defaults to false and does not enable Events or inbox tools.
Legacy `initialize` negotiation and legacy result shapes remain available.
This option changes no principal, workspace, scope, listener or authentication.

For a modern request, send `params._meta` containing
`"io.modelcontextprotocol/protocolVersion": "2026-07-28"` and an object
`"io.modelcontextprotocol/clientCapabilities"` (an empty object is valid).
Metadata is evaluated separately for each request; client identity and
capabilities do not grant bridge authority. `server/discover` requires this
metadata and returns supported versions and the currently enabled capabilities.
Successful modern discovery, ping, tools and Events results contain
`resultType: "complete"` and `_meta["io.modelcontextprotocol/serverInfo"]`.
Modern discovery and tool-list results additionally contain `ttlMs: 0` and
`cacheScope: "private"`: they are immediately stale and cannot be reused across
authorization contexts. Legacy results keep their original shapes.
Unsupported inline protocol versions return `-32022` with the supported modern
version; malformed required metadata returns `-32602` before tool or callback
work begins. Legacy versions remain available through `initialize`.

The opt-in stdio reader accepts at most 16 in-flight envelopes (including the
one serial handler), with a 1 MiB UTF-8 frame limit. Excess requests are refused
without dispatch. `notifications/cancelled` marks a matching request before its
receipt audit is written. Queued cancelled work is skipped; running work may
finish and preserves any already accepted operation, but its response is
suppressed. Cancellation never reverses dispatch or initiates a retry. IDs and
cancellation state stay in memory. EOF drains accepted work; a handler still
active after a 15-second shutdown wait prevents successful shutdown and keeps
the Engine's database and owner lock open.

This follows the [2026 stdio binding](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)
and [per-request versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning).
Official tunnel-client v0.0.21 forwards modern self-contained stdio requests
without a legacy handshake, but this option does not change the tunnel's
server-info advertisement or force ChatGPT task creation onto a modern path.
No sampling, elicitation, task extension or notification stream is advertised.
Actual Cloud subscription and wake behavior still require separate acceptance.

## Optional MCP Events

Add `"events_enabled": true` to the approved private Phase 1 config only when
`"inbox_enabled": true` and `bridge:read` are already enabled. Default/false
exposes no Events methods or capability; Phase 0 remains status-only. Events add
no tool or scope: the inbox tool count remains eight. When enabled, initialize
advertises both `capabilities.events: {}` and
`capabilities.extensions["io.modelcontextprotocol/events"]: {}`. This does not
change legacy protocol-version negotiation; modern handling is the separate
`mcp_2026_enabled` option above. Preserve the existing
owner/tunnel policy and wait until no ledger operation is `accepted` or `running`
before restarting the corresponding facade to activate the configuration.

The implementation follows the [official MCP Events guide](https://developers.openai.com/plugins/build/mcp-events).
Its documented consumer surfaces are Work chats on ChatGPT web, desktop Work +
Cloud, and dots. This does not establish event support or automatic wake for a
local desktop chat. Refresh the intended plugin's capabilities and verify a
subscription and its actual consumer behavior separately from local tests.

- `events/list` advertises `bridge.inbox.message.created`, with webhook delivery
  and an optional `arguments.agent_session_id` filter. Omit the filter for all
  owner-authorized sessions; use only facade-issued opaque `as_` IDs.
- `events/subscribe` takes `name`, `arguments`, `delivery` (`mode: "webhook"`,
  HTTPS `url`, `secret`) and optional `ttlMs` and `cursor`. The signing secret
  must use `whsec_` plus a base64-encoded 24–64-byte key. The callback must pass
  address validation and signed challenge verification before activation.
  Repeating the same owner, callback, name and canonical arguments refreshes the
  existing subscription. The response includes `id`, finite `refreshBefore`,
  `cursor: null` and `truncated: false`. The default lease is one hour and the
  maximum is 24 hours; `ttlMs: null` still grants the finite default lease.
- `events/unsubscribe` takes the same `name`, canonical `arguments` and `delivery`
  (`mode: "webhook"`, `url`), without a signing secret, and stops that subscription.
  The returned subscription `id` is informational, not an unsubscribe argument.

Notifications carry only `data: {"agent_session_id": "as_…"}` with the event
envelope and `cursor: null`. They contain no message body, message handle,
provider UUID or local path. Use `bridge_inbox_list`, `bridge_message_get` and
explicit `bridge_message_ack` to consume independent mail. Correlated replies
remain exclusively in `bridge_operation_get`. Notification delivery performs a
bounded validation read (`inbox_get`, offset 0, `max_chars: 1`) to check unread
owner/route/epoch access. It never consumes or ACKs mail, emits or persists the
body, or sends or retries a bridge message. Agent-authored mail is data, not
authorization.

Webhook requests use Standard Webhooks signatures over the exact serialized
body, event ID and signing timestamp. A `2xx` means callback receipt, not
successful processing or downstream completion. Webhook delivery is attempted
without automatic retries under the local no-retry policy; interrupted or failed
notifications may be missed. This event has no replay: use a fresh inbox list
after a gap, restart or reconnect, and guard downstream actions against duplicate
processing. A subscription does not guarantee a conversation wake.

The watch emits one callback attempt per new independent broker message for each
matching subscription. Existing unread mail is not replayed when subscribing.
The watch queue holds at most 128 events; overflow is a notification gap, while
unread mail remains available through the inbox. `bridge_status` reports Events
health in `data.events`, including watch gaps and callback failures.

`data.events.request_diagnostics` is volatile: `started_at` marks its observation
window, and `methods` has only the fixed `events/list`, `events/subscribe` and
`events/unsubscribe` slots. Each reports `received_count`, `accepted_count`,
`failed_count`, `last_received_at`, `last_stage`, `last_outcome` and nullable
`last_failure` (`at`, `code`, `reason`, `stage`). Stages identify authorization,
validation, route resolution, callback verification, watcher setup, key storage,
subscription persistence and cleanup. Outcomes are `processing`, `accepted`,
`failed` or null. A later success retains the last failure and its original time.
There are no raw parameters, callback URLs, keys or key references, IDs, or
request history in these diagnostics; nothing is persisted, and facade restart
resets the window. They cannot reconstruct attempts from before that window.
For a controlled attempt against the same facade, a received-count increase
shows the request reached Agent Bridge; an unchanged count shows no method
arrival observed in that window. Neither proves a host task was saved, its
notification was processed, or a conversation woke. Host-side errors still need
the exact controlled response: inspect the response body's status (including
`ERROR`) even when an outer wrapper reports `isError: false`.

The separately user-approved receipt audit persists only bounded metadata in
`state_dir/events-audit.jsonl` and its rotated `events-audit.jsonl.1`, each at most
1 MiB and an owner-only 0600 regular file. Writes and rotation are serialized and
fsynced; the existing ledger lock keeps a single facade process. Record shapes:

- `mcp_request`: UTC `at`, `type`, fixed `method`, and fixed `tool` only for
  `tools/call`. Known methods are `initialize`, `notifications/initialized`,
  `ping`, `tools/list`, `tools/call`, `events/list`, `events/subscribe` and
  `events/unsubscribe`, plus `server/discover`, `resources/list`, `prompts/list`
  and `notifications/cancelled`; all others become `other`. Logging these names
  does not implement unsupported methods; `server/discover` is implemented only
  by the separate modern opt-in. Tool names are one of the eight existing tools or
  `other`. All request receipts include normalized `requested_protocol`, taken
  from initialize parameters or the per-request protocol metadata.
- `mcp_response`: a handler-produced response, not proof of transport delivery
  or client acceptance. It records UTC `at`, `type`, fixed `method`, fixed `tool` only for
  `tools/call`, `outcome` (`result` or `error`) and a known JSON-RPC integer
  `error_code` (`-32700`, `-32600`, `-32601`, `-32602`, `-32603`, `-32015`, `-32022`)
  or null. Only an initialize result includes
  `negotiated_protocol`. Protocol metadata uses the public versions `2024-11-05`,
  `2025-03-26`, `2025-06-18`, `2025-11-25`, `2026-07-28`, or normalized `absent`
  or `other`; raw version strings are not stored. Notifications have no response
  audit record. No result bodies, error text or raw capabilities JSON are stored.
- `event_request_outcome`: UTC `at`, `type`, fixed `method`, `stage`, `outcome`,
  normalized JSON-RPC integer `code` or null, and fixed `reason` or null.
- `broker_message_observed`: UTC `at`, `type` and boolean `correlated`. This
  receipt is written before the correlated-message filter only when an already
  active Events watcher observes a broker message. It adds no watchers and has
  no backfill; it is not a complete broker arrival log.

The audit contains no raw arguments, bodies, callback URLs, keys or key
references, live provider IDs, paths, sockets, handles or cursors. It is separate
from routing and correspondence and does not persist the volatile request
counters above. Rotation bounds retained history; it cannot establish what
happened before logging began or outside active watcher coverage. Request
receipt, request outcome and broker observation are separate evidence; none
proves callback delivery or consumer processing. There is no per-callback
delivery trace in this audit. Audit write failure does not change request
behavior; volatile `data.events.audit_write_failures` and `audit_last_error`
(`"audit_write_failed"` or null) disclose logging failures.

Subscriptions survive facade restart in bounded, owner-only
`state_dir/events.json` (0600), separate from routing, correspondence and the
operation ledger. Metadata is bound to the principal, tunnel, ledger namespace
and configured policy. Policy changes revoke persisted subscriptions accepted
under the old policy. Signing keys, including the previous key during a brief
rotation window, live only in the macOS Keychain service
`agent-bridge.mcp-events`; refresh replaces the key and temporarily signs with
both keys. An unavailable Keychain fails closed; there is no plaintext fallback.
This is the narrowly user-approved Events key storage exception, not permission
to persist other credentials. Keep callback metadata and keys out of Git,
routing/correspondence checkpoints and diagnostic bundles.

Deployment, callback verification against ChatGPT, real notification acceptance
and the consumer's response require a separately observed owner-authorized test.
Do not infer them from unit tests or from exposing the Events capability.
