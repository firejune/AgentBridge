---
name: agent-bridge
description: Discover live Codex (ChatGPT Desktop), Claude Code, and Antigravity (IDE & Desktop) sessions and connect project channels to exchange real-time questions, answers, and progress updates. Use when the user asks for cross-agent collaboration, channel binding, session handoff, or bridge operation. Casual mentions or normal coding tasks are not outbound messaging requests.
---

# Agent Bridge

**English** | [한국어](https://github.com/firejune/AgentBridge/blob/main/docs/ko/SKILL.md)

A shared local real-time bridge for **Claude Code / Desktop (`claude-peer`) ↔ Codex / ChatGPT Desktop (`codex`) ↔ Antigravity IDE / Desktop (`agy`)** and CLI participants (`realtime`/`claude`) on the same machine. Any participant can push messages directly into a peer's open live session **without interrupting active work**, enabling 3-way (multi-agent) real-time questions, answers, and progress reporting.

## Registration ownership

`register` protects each `(project, name)`: another transport, session UUID or canonical workspace is refused with `Route name is already in use; use a different name`. Same names in different projects are independent. Exact-binding retries return the existing route without rewriting it. An omitted `--executable` preserves the old value; an explicitly different executable is also refused. Existing channels and message routes stay pinned.

New-session registration commands check an occupied name before opening a session; the broker still checks atomically at final registration. In the Codex result, a known final conflict preserves the created session with `registered: false` and `uncertain: false`; an ambiguous registration write stays uncertain. Neither path automatically removes the registration or retries it. This protection requires the updated broker; client preflight does not reserve a name.

## Entrypoint

Run `scripts/bridge.py` inside this skill directory. Requires Python 3.10+ on macOS/Linux with zero third-party packages.

```bash
bridge_cli="$HOME/.agents/skills/agent-bridge/scripts/bridge.py"
python3 "$bridge_cli" --help
```

If installed elsewhere, use the sibling `scripts/bridge.py` next to the `SKILL.md` you read. Keep all scripts in the same directory; do not copy scripts into target project repositories.

## 3-Way Live Collaboration Workflow

1. **Discover and verify sessions**: Run `sessions` to list live sessions and `session` to verify the exact UUID, title, and working directory (`cwd`). When multiple candidates exist, disambiguate using the current task and user instructions. Never pick a target by recency or fuzzy title matching alone. **If multiple candidates remain ambiguous, ask the user which session to use before sending.**
2. **Check the broker**: Run `ping`. Reuse an existing broker if running. If no broker is running and bridge operation is within scope, start `serve` and confirm the `ready` output.
3. **Register routes and channels**: Inspect `routes` and `channels` first so you do not overwrite another active session's binding. Register live sessions with `claude-peer`, `codex`, or `agy`, and bind project channels via `channel-bind`.
   See the Codex section below for Desktop adoption and startup turn requirements.

   To reach an existing Codex thread, send to its registered route as usual. See the Desktop queue notice below for delivery requirements.

   **Give every session you open a title that identifies it.** The person reads the session list in the desktop apps and has to tell the sessions apart without opening each one. Use `PROJECT/NAME · WORK`, where NAME is the route name and WORK is the card or purpose in a few words, for example `rig-c/naru-49 · create#49 ground ask`.
   - Codex: `register NAME --project PROJECT --transport codex --new` and `open --provider codex --register NAME --project PROJECT` default to `PROJECT/NAME` when `--title` is absent. An explicit `--title` wins. Prefer an explicit `PROJECT/NAME · WORK` title to include the purpose. `open --provider codex` without `--register` has no default title, so pass `--title` to name it.
   - Antigravity: pass `--title` on every `open` and `register --new`.
   - Claude Desktop: `--title` is the pre-filled first message, not the name. The route label `PROJECT/NAME` is written on the first push. Write the WORK part into that first message.
   - Claude IDE: `--title` is ignored, and the label `PROJECT/NAME` is written on the first push.
   - A session that was opened without a title gets one before its first brief is sent. For a claude-peer route, use `title NAME --project P`. For other providers, rename the session in its app, or ask the person to.

4. **Push directly (no polling loops)**: Only `realtime` CLI workers use `watch`. **`claude-peer`, `codex`, and `agy` push directly into open live sessions without a `watch` polling loop or spawning duplicate CLI processes.**

```bash
python3 "$bridge_cli" sessions --cwd /absolute/project  # Add --with-model when live model details are needed
python3 "$bridge_cli" session claude SESSION_UUID
python3 "$bridge_cli" session codex SESSION_UUID
python3 "$bridge_cli" session agy SESSION_UUID
python3 "$bridge_cli" ping
# Start only when no broker is running:
python3 "$bridge_cli" serve
```

### 1. Claude Code / Desktop (`claude-peer`)

```bash
# Register an existing live Claude session:
python3 "$bridge_cli" register claude-agent --project PROJECT --transport claude-peer \
  --session CLAUDE_SESSION_ID --discover

# Open a new Claude Code IDE session autonomously (--host ide):
python3 "$bridge_cli" register claude-worker --project PROJECT --transport claude-peer \
  --new --host ide [--ide antigravity-ide|vscode|cursor] --cwd /path/to/workspace --timeout 120

# Open a new Claude Desktop session (prefills composer; waits for user Enter):
python3 "$bridge_cli" register claude-worker --project PROJECT --transport claude-peer \
  --new --cwd /path/to/workspace --title "First message for the new session" --timeout 300
```

- **Direct push**: `--discover` locates the exact `sessionId` in `~/.claude/sessions/<pid>.json`, verifies the process is alive with `peerProtocol == 1`, and reads its `cwd`. `send`, `ask`, and `reply` write directly to the session's messaging socket (`submitted`), arriving on the next turn with the exact `bridge.py reply <ID> …` command on the final line.
- **Failure handling**. Pre-write errors leave the message `pending` with a `push_failed` event. A write or close-wait failure after writing starts records `uncertain`. `deliver ID --timeout SECONDS` bounds lookup, claim and peer I/O. After a possible write, the broker gets up to 10 more seconds to record the result. If the broker loses a guarded claim reply before any peer write, the client waits for claim admission to close and gets up to 2 more seconds to release only its reservation. A confirmed release leaves the message `pending` with a `push_failed` event. An unconfirmed claim, release or finish reports the message ID and phase. Inspect `show ID` before resolving.
- **Queued deliveries**. `show` and CLI `inbox` identify `pending` messages blocked by another dispatch using `blocked_by` and a `pending behind <id> since <time>` note. A `claude-peer` push behind a `dispatching` delivery (such as a wake) waits for it once within its budget; it is pushed once if that delivery ends delivered, and otherwise keeps that note with the blocker's end state (`… (<state>); not sent`). Before using `resolve` on a blocked dispatch, verify that the originating delivery client has stopped and inspect the exact receiving session. No uncertain push is retried automatically. The `peerToken` key is read in memory only.
- **Idle-closed sessions (wake)**: Claude Desktop pauses a hidden session 900 s after its last turn and evicts idle sessions under memory pressure; the record and socket disappear. A push to that route first opens `claude://claude.ai/epitaxy/<local_id>`, waits for the same `sessionId` to come back with an inbox socket (up to 30 s, inside the delivery budget, keeping 5 s for the push), then pushes once (`wake: woken` in the result). `unsupported`, `failed` (the opener reported the link was not opened) or `uncertain` returns the message to `pending` with nothing sent; do not loop on it. The outcome stays on the message as `wakes` (state, fixed `code`, `elapsed`, `at`, `source`, `attempt`; readable with `show` after it is answered or done, and across restarts); the free-text reason is only in the volatile note. Check explicitly with `wake NAME --project P` (`woken` / `already-live` exit 0, `unsupported` / `failed` / `uncertain` exit 1). The link focuses that session in the app and may bring the app forward. Scheduled-task sessions and IDE sessions are `unsupported`.
- **Opening new sessions**:
  - **IDE (`--host ide`)**: Focuses/opens the workspace window via the IDE CLI, opens `<scheme>://anthropic.claude-code/open`, and binds the single new `entrypoint == "claude-vscode"` record under `--cwd` with no human step.
  - **Desktop (default)**: Opens `claude://code/new?folder=…&q=…` and waits up to `--timeout` for a new `entrypoint == "claude-desktop"` record (`hostSessionId: "local_…"`) after a person presses Enter.
  - `--model` is rejected on new Claude sessions; select the model in the UI or via `model --set` on Desktop.
  - **Session label**: registering a claude-peer route writes `PROJECT/NAME` as the session's custom title, which the session list shows instead of the shared injected first line. It never replaces an existing title. A `--new` session is labelled on its first push, once its transcript exists. The IDE list reads only the first and last 64 KiB of a transcript, and the running CLI adopts the label only when it reads its own tail (after about 32 KB of its writes). So every push re-appends the label while it is outside the last 64 KiB, until the CLI re-emits it itself. Use `title NAME --project P` for routes registered earlier. On `--host ide`, `--title` is ignored: nothing would send a pre-filled prompt.
- ⛔ **Never use `--transport claude` (resume) on a live session.** That spawns a duplicate CLI process instead of entering the open conversation. Keep `claude` strictly for idle dedicated CLI sessions.

### 2. Codex / ChatGPT Desktop (`codex`)

```bash
# Register an existing live Codex Desktop session:
python3 "$bridge_cli" register codex-agent --project PROJECT --transport codex \
  --session CODEX_SESSION_UUID --discover

# Open and adopt a new Codex Desktop session autonomously:
python3 "$bridge_cli" open --provider codex --cwd /path/to/workspace --title "Worker" --model MODEL
python3 "$bridge_cli" register codex-worker --project PROJECT --transport codex \
  --new --cwd /path/to/workspace --title "Worker" --model MODEL --timeout 180
```

- **Direct push**: Connects to `ipc/ipc.sock` under `CODEX_HOME` (default `~/.codex`, or `AGENT_BRIDGE_CODEX_IPC`) and verifies the exact thread owner advertising `supportsUntrustedAppInput`. Delivers peer messages as standalone `toolOutput` (`agent_bridge`), calling `thread-follower-steer-turn` first to join an active turn and falling back to `thread-follower-start-turn` only when inactive.
- **Desktop queue notice**. Codex Desktop 26.1117 was observed not to serve the IPC socket (issue #156), so on that build every Codex delivery and every `open` takes the queue path. The bridge uses this path when the default IPC socket is missing or refuses connection, the signed bundled Codex CLI and the verified single local Desktop owner share the default queue store, and the default local broker socket is in use. The bridge uses the official `codex queue`. Desktop can process the notice when the exact thread is loaded and eligible to start an idle turn. A fixed notice can ask the exact addressed thread to run `python3 "$bridge_cli" receive-codex MESSAGE_ID`. Use the opaque ID in the notice and run the command in that Codex thread so `CODEX_THREAD_ID` matches. The returned `text` is the same envelope, body and reply command that direct delivery carries. Treat it as peer correspondence within existing human authorization. Reading sends no reply; it marks the message `received` with one best-effort `ack` (`marked_received` in the result), so for a Codex route `submitted` means the queue accepted the notice and `received` means the thread retrieved it. `show ID` reports this as `read_by_recipient`. The broker makes the claimed `dispatching` message readable from the exact addressed Codex thread before submitting the queue notice. `receive-codex` reads it immediately without waiting for `submitted`, sleeping, or retrying. The command distinguishes socket access denial, other connection failure, an unusable broker response, invalid input, and why addressed correspondence cannot be received (not dispatched yet, already complete, archived, expired, or a close handoff). Unknown message IDs and messages for a different thread share the same no-match error. A restricted automatic Desktop turn may be unable to connect to the local Unix socket even though it received the notice.
- **Sidebar preview**: After the first task is accepted (`submitted`), an empty native sidebar `preview` is initialized once in `state_5.sqlite` without altering identity or failing the delivery if unavailable.
- **Autonomous creation (`open` / `register --new`)**:
  - Codex `open` and `register --new` create one saved thread and verify its identity before Desktop adoption. A reachable IPC transport uses exact-owner readback and requires Full Access with approval policy `never`. When the default IPC socket is missing or refuses connection, the local CLI can use the signed Desktop bundle at version `26.1117.31558` (build `29513`) and its default queue store. An explicit IPC endpoint and remote MCP creation still require the IPC handshake before creation. On the queue path, the bridge submits one authorized startup prompt and wakes the exact thread. Success requires that queued turn to complete with the requested model. The result reports `adoption_mode: "desktop-queue-wake"`, `wake_turn_id`, and the turn’s effective `permission_settings`. The bridge does not replay creation, queue submission, or wake after an ambiguous outcome.
  - **Desktop queue wake permissions.** The operator can add `"codex_desktop_wake_permissions": "workspace-write"` to the local `<state-dir>/policy.json`. For a newly created local Codex Desktop thread that uses the queue wake path, the bridge saves `:workspace` with approval policy `on-request` after the rollout exists and before submitting the startup prompt. The profile allows writes in the exact workspace and default OS temporary locations and restricts network access. The bridge verifies the exact queued turn used those settings before reporting success. Subsequent queue turns on that thread inherit those permissions until the thread’s settings change. With the key omitted, the bridge saves Full Access with `never` for new queue-wake threads as before. IPC adoption still requires Full Access with `never`. Sending a queue notice to an existing thread uses that thread’s settings and does not change them.
  - Model selection follows `--model` > `AGENT_BRIDGE_CODEX_MODEL` > `policy.json` `defaults.codex` > `gpt-6.2-sol`.
  - Ordinary `open --provider codex --title TITLE` refuses duplicate unarchived threads matching the same title and `cwd` (`stage: "duplicate"`); pass `--new` to bypass duplicate detection. Never automatically repeat a failed or `uncertain` creation. When a thread was created and no route points at it, the result's `recovery` names the exact thread and the one command that binds it (`register NAME --project P --transport codex --session UUID --discover`). When the requested name is taken, `route_conflict` gives the holder's transport, session and workspace, and nothing is created. An unreachable broker is reported as such before any thread exists.

Codex provider failure observation checks every registered Codex thread, with 10 seconds between passes. Configure `serve --codex-failure-interval SECONDS` (`0` disables observation). It pins the registered thread, current Desktop owner and latest terminal turn, and requires an idle runtime, the latest turn's `failed` completion and a matching terminal error with `willRetry: false`. Known network/stream failures produce one normalized `provider_failure_detected` notice to the originating message's frozen return route. Inspect the owner's `inbox`, then `show ID`, or read the notice through the MCP inbox. The original task keeps its state. Completed tool calls remain eligible for detection.

The notice records `observed_at`, the turn ID and failure kind. `failed_at` is derived from turn start plus duration when available and carries `failure_time_source: turn_duration`, otherwise it is null. Provider text, tool output and Desktop owner metadata stay out of the notice/checkpoint. Native capacity retries stay with the app. Observation resumes from registered routes after a broker restart. With no task in flight, the notice goes to the last retained sender to that route. If no sender remains in bounded correspondence, it stays in the registered thread's own inbox. Deduplication survives restart while the notice remains in bounded correspondence. Missing owners, unsupported snapshots and inactivity provide no provider-failure signal.

Codex recovery uses the native Resume continuation with the existing thread ID, empty `input`, `turnTrigger: "resume_interrupted_task"` and inherited thread settings. The bridge has no composer. It omits `additionalContext` and marks writing-block context prepared, matching Resume with an empty composer. The default minimum delays are 15/30/60 seconds before up to 3 accepted attempts. Configure `serve --provider-retry-backoff 15,30,60 --provider-retry-cap 3`. A shorter schedule reuses its last delay. Polling can add one observation interval, and policy is fixed for each failure episode.

Before each write, recovery rechecks the pinned owner, latest failed turn and input fingerprint, idle state, terminal `willRetry: false`, outstanding requests and tool completion. Completed tool calls are allowed. A newer turn, intervening input, owner change or started call without output stops with `provider_retry_unsafe`. Each attempt is reserved durably before IPC and appears as a `provider_retry` k/N message in the original detection notice's conversation. Accepted attempts count toward `provider_retry_exhausted` when the cap's continuation also fails. An ambiguous write, or restart with an unresolved reservation, stops as `provider_retry_uncertain` and is never resent. A later completed or interrupted turn resets the counter. Aliases of the same Codex thread share the counter. Normalized reservation/count/fingerprint metadata persists privately with correspondence, while the notice and event bodies stay immutable. Active episode counters remain protected from retention until reset. A known lost correspondence checkpoint stops recovery as `provider_retry_uncertain`.

### 3. Antigravity IDE & Desktop (`agy`)

```bash
# Discover and register an existing Antigravity IDE or Desktop session:
python3 "$bridge_cli" sessions --provider agy --cwd /path/to/workspace
python3 "$bridge_cli" register agy-agent --project PROJECT --transport agy \
  --session AGY_CONVERSATION_ID --discover

# Open a new Antigravity Desktop or IDE (--host ide) session autonomously:
python3 "$bridge_cli" open --provider agy [--host ide] --cwd /path/to/workspace --title "Worker"
python3 "$bridge_cli" register agy-worker --project PROJECT --transport agy \
  --new [--host ide] --cwd /path/to/workspace --title "Worker"
```

- **Direct push**: Connects to local Antigravity `language_server` / `agy --hub` instances over ConnectRPC, covering **both Antigravity IDE side-panel sessions and Agent Manager (Desktop) sessions** without a VS Code extension. When a conversation is cached across multiple servers, the bridge selects the active (`running`) or freshest owner (`stepCount`, `lastModifiedTime`, `--add-dir` workspace affinity) and automatically reaps idle orphaned `agy --hub` processes (`ppid == 1`).
- **Steering & wake**: Injects into the conversation's message hub (`SendAgentMessage`, `MESSAGE_PRIORITY_HIGH`). If `running`, it joins the active turn on the next step as `<SYSTEM_MESSAGE>[Message] ...` without cancelling work. If `idle` (or waiting on background tasks/subagents), it follows up with an empty `SendUserCascadeMessage` (`NEXT_INVOCATION`) to wake the session immediately. Because Antigravity's built-in `send_message` only addresses subagents, reply via `bridge.py reply` in the shell.
- **Autonomous creation (`open` / `register --new`)**:
  - **Identical across Desktop and IDE (`--host desktop` / `--host ide`)**: Calls `StartCascade` + `UpdateConversationAnnotations` on the unified Antigravity server (`~/.gemini/antigravity`), automatically links or creates the project (`~/.gemini/projects/*.json`) and worktree environment (`projectEnvConfig`), and initializes `customAgentSpec` so the 0-step session wakes with all built-in tools on the first push.

Antigravity (`agy`) provider failure observation checks every registered `agy` route with one `GetAllCascadeTrajectories` poll per live server every 10 seconds (`serve --agy-failure-interval SECONDS`, `0` disables observation) and fetches the tail step (`GetCascadeTrajectorySteps`, `stepOffset = stepCount - 1`) only when the trajectory is `IDLE` and `stepCount` changed. Internal retry attempts (`1–8`, while `RUNNING` or without `shouldShowUser: true`) are ignored. Terminal user-visible errors (`shouldShowUser: true`, `CORTEX_STEP_STATUS_DONE`, no trailing step) are deduplicated by `(sid, step_index, errorId)` with correlation `agy:<step_index>`: a terminal step matching `WAKE_ELIGIBLE_ERROR_SUBSTRINGS` (`"http2: timeout awaiting response headers"`) arms the shared `15/30/60` s, cap `3` retry policy (`provider_retry` k/N, `provider_retry_exhausted`, `provider_retry_unsafe`, `provider_retry_uncertain`) and wakes via `wake_session` (empty-items `SendUserCascadeMessage` with `NEXT_INVOCATION`), while any other terminal `shouldShowUser: true` error records `provider_failure_detected` only and does not wake. With or without an in-flight message, notices go to the last retained sender to that route (or the route's own inbox if no sender remains). If `stepCount` has not advanced and `status` is not `RUNNING` on the next poll window after a wake, the monitor records `provider_retry_uncertain` and never double-wakes. In-memory tail-step caches reset on broker restart.

### 4. Sending, Replying & Closing Sessions

```bash
# Send or ask (waits up to --timeout; exit code 2 means timeout, not failure):
python3 "$bridge_cli" ask --project PROJECT --from-channel logic --channel view \
  --key UNIQUE_REQUEST_KEY --file /absolute/question.txt --timeout 60

# Reply on the original return route:
python3 "$bridge_cli" reply REQUEST_ID --key UNIQUE_REPLY_KEY --file /absolute/answer.txt

# Close a finished worker session and remove its route (add --archive to archive):
python3 "$bridge_cli" close NAME --project PROJECT --retro-file /absolute/retro.txt [--archive] [--keep-route] [--timeout 10]
```

- 📬 **Always report results back on the channel the task came in on** via `bridge.py reply`, including when blocked. Issue or PR comments do not replace the bridge reply.
- `ack` and `progress` report receipt and status, not final answers. Exit code 2 from `ask` or `wait` means the wait timed out — run `wait REQUEST_ID` again rather than resending.
- Close sessions you own or have the owner or HQ’s authorization to close. Plain `close` stops the exact Claude CLI process, detaches an idle Antigravity conversation or detaches the Codex route. Pass `--archive` to archive the session.
- After archive is confirmed, the broker removes eligible retained correspondence for the exact session, including aliases, and expires inbox snapshots affected by removal. Provider transcripts remain available. Ordinary close preserves correspondence. Direct archive with `--keep-route` still cleans correspondence.
- By default, direct Codex `close --archive` verifies the saved archive state. An already open Desktop sidebar may still show the chat. For a native Desktop archive, use `close --archive --handoff` and archive the exact thread in Desktop or use an enrolled operator. The owner can set `"prefer_native_for_direct": true` in `close-operator.json` to make direct `close --archive` hand off the root after archiving its reviewed descendants. This preference applies when the route will be removed. For direct closes that select this preference, the default local broker socket can notify the enrolled operator through the verified Desktop queue if the default Desktop IPC socket is missing or refuses the connection. The operator chat must be loaded and eligible to start a turn. Explicit `--handoff` uses IPC or manual archive in Desktop. A pending direct-close handoff reports `awaiting_native_archive` and exits 1. Use the operator receipt or `close --finish CLOSE_ID --expect-session UUID` to verify completion. [Closing a session](references/usage.md#closing-a-session).
- Direct Codex `close --archive` checks registered routes and process working directories. The saved-state path verifies archive bits before removing the route. `archive-sweep --cwd-prefix /absolute/workspace` previews candidates and refusals. Review its private JSON, then use `archive-sweep --apply --manifest FILE`. Undo may unarchive descendants too. [Codex archive sweep](references/usage.md#codex-archive-sweep).

- If saved metadata confirms that the exact Codex recipient is archived, the CLI reports `state: "failed"` and `reason: "archived"` and exits 1 before contacting Desktop.
- If the exact Desktop owner cannot be located for delivery, the message stays `pending`. The CLI and MCP report the reason and any verified missing saved workspace. The bridge never reopens the thread or resends on its own.

## Models: Read, Switch, Policy

```bash
python3 "$bridge_cli" model NAME --project PROJECT              # Read fresh at query time
python3 "$bridge_cli" model NAME --project PROJECT --set MODEL  # Switch and verify readback
python3 "$bridge_cli" routes --project PROJECT                  # Includes "model" per route (--no-model skips)
python3 "$bridge_cli" policy                                    # Show enforced defaults and denylist
```

- **Policy**: Stored in `<state-dir>/policy.json` (e.g., `{"defaults": {"agy": "gemini-4-argon-medium"}, "deny": {"codex": ["gpt-6-astra"]}}`). Only a human edits `policy.json`. While absent, the seed denylist `{"codex": ["gpt-6-astra"]}` applies.
- **Antigravity (`agy`)**: Reads the last generator's `plannerConfig`. Switching is accepted while idle, preserves existing planner fields, and runs **one real turn on the new model** (2–8 generators, 10–90 s; reports `generators_spent` and `busy_after`). Do not resend if `busy_after: true`; wait for idle.
- **Codex (`codex`)**: Reads and updates the exact Desktop owner's **next-turn model settings** (`thread-follower-update-thread-settings`) without interrupting an active turn.
- **Claude (`claude-peer`)**: Reads the newest assistant message in the transcript. Desktop switching relays `set_session_model` through a `command_post` route configured in `policy.json` (`{"command_post": {"claude": "<route>"}}`); the switch result (`applies: next turn`) is authoritative even while transcript reads still show the previous turn's model. VS Code / IDE model switching is `unsupported (STOP)`.

## Checking Usage Limits

```bash
python3 "$bridge_cli" usage [--provider all|codex|agy|claude] [--since 6h] [--refresh] [--json]
python3 "$bridge_cli" usage --events
```

- Check your own session's built-in usage tool first when available; query `bridge.py usage` when missing/stale or when checking another provider/account or burn trend.
- `usage` defaults to `--provider all` (Codex account buckets + Antigravity `agy/gemini-weekly`, `agy/gemini-5h`, `agy/partner-weekly`, `agy/partner-5h` buckets + Claude `claude/desktop` and `claude/ide` buckets) and ends each capacity-judged line with `holds until reset`, `AT RISK: empty ~T before reset`, `EXHAUSTED`, or `risk unknown`.
- Antigravity (`agy`): reads `RetrieveUserQuotaSummary` (`{}` using the language server's 5-minute cache; pass `--refresh` to send `{"forceRefresh": true}` on demand — the background collector never sends `forceRefresh`). `3p-*` buckets map to `partner-*`, and legacy `agy/gemini` and `agy/partner` records remain readable as history. Missing weekly figures report `weekly: unknown` (never `0%` or `100%`). For `agy`, `AT RISK` / `holds until reset` is judged on the weekly buckets (`agy/gemini-weekly`, `agy/partner-weekly`); the 5-hour line is shown next to it but is never the capacity verdict.
- `--events` prints reset events and two horizons: plan spending to the **spend horizon** (earlier of official reset and estimated early reset), and judge stopping risk by the **stop risk horizon** (official `resets_at`).
- Claude: `claude/desktop` is the Claude Desktop account (Desktop's own history; used % only, no reset time), `claude/ide` is the Claude Code login used by the IDE extension and CLI (live usage endpoint via its Keychain token, else its `~/.claude.json` cache). A Desktop HQ reads its IDE sessions' limits here, and an IDE session reads Desktop's. Neither read starts a model turn. Burn and risk use only the account the latest read saw (an opaque hash; samples of another account are never blended). Inside a Claude Code session `usage` prints a warning first: read your own Claude limits from the app (`get_usage`), not from the bridge.
- Every `send`, `ask`, and `reply` touching a `codex` or `agy` route (or between two `claude-peer` routes) includes a `usage` summary in the sender's output (`ok`, `at_risk`, `exhausted`, `unknown`). Pace or reassign work when `at_risk` or `exhausted`.

## Agent Worktrees

- Write code only in a worktree under `<workspace>/.worktrees/<repo>/<agent>-<issue>`; keep the main checkout on `main`.
- Create with `bridge.py wt new REPO AGENT ISSUE [--slug SLUG]` (or `--detach REF` for a read-only pin).
- Inspect with `bridge.py wt list --text` and preview cleanup with `bridge.py wt sweep` (run `--apply` only when the owner asks). Remove your own worktree right after merge with `git worktree remove PATH` and `git branch -D BRANCH`.

## Remote MCP Facade

A confirmed route/session conflict before broker posting returns `BACKEND_UNAVAILABLE` with a fixed `ROUTE_CHANGED:` message. Initial acceptance returns a tool error without creating an operation; a conflict detected after acceptance ends that operation as `failed`, releasing its slot. The facade never changes the route or resends. A route mismatch discovered after the broker send was attempted remains `unknown`.

- Local agents always use `scripts/bridge.py` directly via the Python CLI; do not register `mcp_server.py` as a local MCP server.
- `scripts/mcp_server.py` is for a single private remote tunnel (`--phase1-config` over `stdio`): see [remote-mcp.md](references/remote-mcp.md).
- Independently keyed MCP requests can queue for the same authorized session while it is busy or awaiting replies. Submission order is preserved per exact UUID and transport. Each operation retains its own result. An ambiguous write quarantines only that destination and can return `SESSION_BUSY`. Unknown operations are never resent and count toward the 64 active/unknown operation limit.
- Use `bridge_message_reply({message_id, message, idempotency_key})` to answer independent inbox mail when the connection has `bridge:message:send`. The signed inbox ID selects the original sender’s frozen return route. Reply and ACK are separate actions.
- For remote session creation, use `bridge_models_list` with an approved workspace alias and the intended provider. Optionally pass the exact ID as `model` for Codex or Antigravity. Omit `model` for existing defaults. [Session model selection](references/remote-mcp.md#session-model-selection).
- Remote MCP Events require `events_enabled: true`, `inbox_enabled: true` and `bridge:read`. A matching signed `bridge.inbox.message.created` webhook can wake a supported ChatGPT Work chat on web or Desktop Work with Cloud selected. Read a captured reply with `bridge_operation_get` when the event carries `operation_id`. Read independent mail with the inbox tools and ACK it explicitly. After a gap, list the inbox and query known operations. Notifications have no replay or automatic retry. [Events contract and setup](references/remote-mcp.md#optional-mcp-events).

### Reporting to the remote coordinator

The session connected through the remote MCP facade (the remote coordinator) is not a local session. It does not appear in `sessions` for any provider or in a desktop app's session list; do not look for it there or conclude it is absent.

- **Answering it**: when the work came as a bridge message, `reply` to that message. The reply goes to the address it wrote from.
- **Reporting unprompted** (a stage finished, or you stopped):
  1. Find its route for your job: `routes --project P`, then the `realtime` routes with no session whose name has the prefix the owner configured for coordinator routes (`inbox_route_prefixes`) and whose `cwd` is your work folder or one of its parents.
  2. Send only when exactly one route matches. With none or several, stop and ask the user; never choose by a similar name. A route of another agent working on the same job is not the coordinator.
  3. Register this session in that project if it has no route there (`register NAME --project P --transport … --session … --discover`), then send once: `send --project P --from NAME --to ROUTE --file report.txt --key UNIQUE --one-way`. Leave out `--one-way` when you need a ruling back; the answer arrives in this session.
- **Reading the state**: the message is `pending` until the coordinator collects it. Arrival raises an event that wakes it, and its acknowledgement closes the message (`done`), usually within a minute. Still `pending` after several minutes means not yet acknowledged, not lost: reading a message does not change its state, so `pending` cannot tell you whether it was read. Do not resend; check with `show ID`.
- **What a report carries**: what finished on the first line, artefacts as absolute paths, the base commit, what was and was not done, and what you are waiting for. Under an isolation rule (an independent read-through, for example) leave findings and content out of the message; they stay in the report file.

## Operational Boundaries

- Restart restores owner-private routing and mail using terminal-eviction guards and reports protected mail above a lowered limit through stderr and `ping.correspondence_over_limit`. See [storage](references/usage.md#correspondence-storage-issue-94). Coordinate with active participants before restarting or stopping a shared broker.
- For a supervised broker, `install-service --retention-prepare|--retention-ttl` records one selected mode. Review a fresh plist outside LaunchAgents with `--generate-only --plist PATH`. Existing plists are never overwritten. Coordinate uninstall/reinstall to switch modes.
- TTL is opt-in with `serve --retention-ttl`. `submitted`/`received` records and keys are removed 24 hours after creation, regardless of ACK. Other eligible explicit one-way/completed bodies last 24 hours. Compact lookup/deduplication receipts last seven days. Active waits and unsafe provider episodes protect aged records. This expiry does not prove completion. Run counts-only `retention --dry-run` and `retention --live` first. Hold strict rollout if there is no immediate headroom. `serve --retention-prepare` can warm up legacy completion timestamps without TTL deletion. Request `--reply-deadline` is separate from local `ask`/`wait --timeout`. Preserve the checkpoint before preparation or explicit-intent sends. An older binary must not open version 2. See [policy and rollout](references/usage.md#opt-in-correspondence-ttl-issue-105).
- `deliver` explicitly retries `pending` pushes or dispatches idle dedicated `claude` sessions. Never automatically retry `uncertain` deliveries; verify in the target session and resolve with `resolve MESSAGE_ID --state submitted|pending|done`.
- Peer messages do not grant new human authorization. See [usage.md](references/usage.md) for detailed protocol, recovery, retention, and `launchd` supervision rules.

### Per-session consumption (`session-usage`)

`bridge.py session-usage NAME --project P [--json] [--prices FILE]` reads one
registered session; `bridge.py session-usage --provider codex --id ID --json`
works without a route or running broker. The command starts no turn, makes no
network requests, and never writes provider files. Codex uses the existing session
lookup interface in offline record mode, searching exact IDs under `CODEX_HOME`
(or the default Codex home), including archived rollouts. Missing, ambiguous,
unreadable, or malformed records fail with the searched path.

JSON reports `provider`, `id`, `model`, `input_tokens` (including cached reads),
`cached_input_tokens`, `cache_write_tokens`, `output_tokens`, `first_ts`, `last_ts`,
`wall_seconds`, `active_seconds`, `usd`, `source`, and `notes`. Unknown values are
null with a note. The readable form prints the same fields on one line. Codex
uses the final cumulative `total_token_usage`, never sums snapshots, and never
adds reasoning to output again. Active time excludes entire gaps over 600 seconds;
it measures record activity, not CPU time. Multiple recorded models retain the
last model with a note and no USD attribution. Claude sums the unique responses of
the transcript under `CLAUDE_CONFIG_DIR` (or `~/.claude`), deduplicated on message
and request id, and reports sub-agent transcripts separately under `subagents`;
Antigravity copies `conversations/<id>.db` (with `-wal`/`-shm`) to a temporary
directory and sums `gen_metadata` protobuf rows deduplicated by response ID `#11`
(skipping all-zero rows and verifying `#9 + #10 == #3`), normalising
`input_tokens = #2 + #5 + #4`, `cached_input_tokens = #5`, `cache_write_tokens = #4`,
and `output_tokens = #3` while leaving timestamps null and noting that retry-usage
inclusion in `gen_metadata` is unknown.

`--prices FILE` accepts a Markdown table headed `model id | input | cached input |
output`, with USD per million tokens. There is no default price file. USD is
`((input - cached) * input_price + cached * cached_price + output * output_price)
/ 1000000`; missing model rows leave USD null. This is the supplied table's formula,
not a subscription invoice. Provider lanes extend `read_claude(sid)` or
`read_agy(sid)` in `session_usage.py`, returning the same shape (`empty` is a helper),
without changing CLI routing or the Codex reader.

The cumulative and reasoning interpretation follows the [Codex session source](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/mod.rs)
and [response usage conversion](https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/sse/responses.rs).

Forks/subagents subtract inherited cumulative snapshots only with a recorded
`subagent_history_start_ordinal` boundary or an exact parent-snapshot prefix match.
An unavailable/unverifiable parent baseline (including unsupported referenced
`history_base`) yields null token fields with a note; timestamp-burst guesses are
not used. `token_usage_record` entries are not added to cumulative snapshots.
`scope: "session-alone"` and `subagents: null` reserve separate sub-agent figures;
a provider reader may fill `subagents` with independently scoped result objects.
Each reader documents its cached-input normalisation in notes and fixture tests.
See [Codex history boundaries](https://github.com/openai/codex/blob/main/codex-rs/thread-store/src/types.rs)
and the [ccusage replay reader](https://github.com/ccusage/ccusage/blob/main/rust/adapters/codex/src/parser.rs)
(the latter also uses timestamp heuristics, intentionally not adopted here).

`ping` also reports `build` (a digest of the modules the broker started from) and `cli_build` (the same digest for this CLI). When they differ, or the broker reports none, `ping` adds `warning: BROKER_BUILD_SKEW …` and still exits 0. A command the broker refuses with `Unknown operation` carries the same warning.

`ping` includes `checkpoint_last_save_seconds`, `checkpoint_max_save_seconds` and `checkpoint_bytes`: the last save attempt duration, the maximum since broker start (seconds, including failed attempts), and the payload bytes of the last successful `correspondence.json` save (excluding the metadata index). Without a checkpoint store, these are `null`, `0.0` and `0`. A save taking more than 1 second emits one volatile JSON stderr line with `event` set to `checkpoint_save_slow` and fields `timestamp` (UTC), `duration_seconds`, `thread_cpu_seconds`, `bytes` (last successful payload size on failure), `retained_count`, `outcome` (`saved` or `failed`) and `stages_seconds` (validation and serialization, manifest writes, and payload writes). Timing covers validation, serialization and durable writes, not mutation staging. The measurements reset on restart. During a save, `ping` reports the last completed attempt and committed message counts. Watch events appear after the corresponding save commits. Mutations, inbox snapshot operations and wait registration queue behind an in-progress save. Shutdown finishes an admitted commit before releasing the state lock. On macOS, `ping` also reports `codex_delivery`, which the CLI checks within about 2 seconds without sending anything to Codex: `path` is `ipc` (the legacy socket accepts a connection), `desktop-queue` (the default queue store is owned and has a single Desktop owner) or `unavailable`, and `reason` names the failed check (`null` otherwise). Another app-server holding the queue store, such as a tool running `codex app-server`, reads as `no single Desktop queue owner`.

`ping` also includes `attention`, a read-only summary of work that is not progressing: `pending_over_hour` (pending messages to a pushed recipient, `codex`, `claude-peer` or `agy`, created more than an hour ago; mail waiting for a `realtime` recipient to collect is not counted), `oldest_pending_seconds`, `dispatching`, `oldest_dispatching_seconds` and `uncertain`. The ages are whole seconds, or `null` when there is no such message. It holds counts and ages only, never message IDs, bodies, routes or names.

Codex `open` / `register --new` does not register a route when permission verification fails. A one-line stderr summary reports success or failure, the stage, and the observed `approvalPolicy` and sandbox type.


### Project retrospectives and closing (#139)

`close NAME --project P` requires `--retro-file FILE` or `--no-retro "reason"`.
Missing retros (`RETRO_REQUIRED`), blank files (`RETRO_EMPTY`) and blank opt-out
reasons (`RETRO_REASON_EMPTY`) are refused before any provider action or route
removal. Towers and HQ use an explicit `--no-retro` reason. A retro is read and
validated before the provider is touched and saved only after the close is
confirmed. The saved-state archive path records it after readback. Explicit and configured native handoffs record it before preparing the handoff because a closing route cannot send the retro notification. A close that fails before that point
saves none and reports `retro_recorded: false`; repeating it later records once.
If the session closed but the save failed, `retro_error` names it; add the
record with `retro add`.

```bash
python3 "$bridge_cli" retro add --project P --card repo#139 --runner worker \
  --model MODEL --from-file /absolute/retro.txt --urgency repeat --tags delivery,tests \
  --session SESSION_ID --notify hq
python3 "$bridge_cli" close worker --project P --retro-file /absolute/retro.txt \
  --card repo#139 --urgency note --tags tests
python3 "$bridge_cli" close tower --project P --no-retro "tower session"
python3 "$bridge_cli" retro list --project P --tag tests --since 2026-11-01 --urgency repeat --json
python3 "$bridge_cli" retro tally --project P --since 2026-11-01
```

Urgencies: **stop** means work stopped or a wrong thing shipped; **repeat** means
the same failure was seen before; **note** covers everything else. The tag
vocabulary is the caller's. `--tower` is optional. Close takes runner and session
from the registered route and reads the model through the existing model adapter;
an unavailable model is `unknown`. Opt-outs have null text and model, with the
reason in `no_retro`. Close's optional `--card` defaults to null, never an inferred
card. Standalone add takes `--session`, defaulting to `CODEX_THREAD_ID` or
`CLAUDE_CODE_SESSION_ID` when available (otherwise null).

Each project uses `<state-dir>/retros/<project>.jsonl`, with a 0600 file, exclusive
append lock and fsync; readers take a shared lock. `--store FILE` overrides
`AGENT_BRIDGE_RETRO_STORE`, which overrides that default. An override may share a
file across projects; queries still filter by project. Project names must be a
single ASCII name, not a path. `--state-dir DIR` must match the broker's custom
state directory when it differs from the socket path with `.state` suffix.
Entries are append-only, containing UTC time, project, tower, card, runner, model,
urgency, tags, verbatim text and session ID. This separate local ledger intentionally
retains retros; it is not bounded correspondence or part of the routing snapshot.
Keep the ledger and session IDs out of Git unless the owner explicitly arranges
private ledger storage. Add/close receipts expose the store path and record ID,
not the retro text. `list` is the explicit local read command; `--json` prints full
records. Tally prints tag, count, first/last date and unique cards. `--since` accepts
an ISO date or timestamp (timezone-free values mean UTC).

No HQ hierarchy is inferred. `--notify ROUTE` selects a registered route in the
same project. The optional owner-edited `<state-dir>/policy.json` setting
`{"retro_notify":{"P":"hq"}}` provides a per-project default. Other policy
settings are preserved. Only **stop** and **repeat** send one message, using the
registered runner as sender, with card, runner, urgency, tags and at most the first
five lines / 1000 characters. **note** sends nothing. With no configured target,
no message is sent. A missing notification sender/target is refused before saving
or closing. Delivery is attempted once; its ID/state or error appears in the
receipt. A notification error does not undo the saved retro or prevent close;
inspect that receipt and message before any manual follow-up. Delivery acceptance
is not HQ action. No other route receives the text. No broker changes or restart
are needed: these commands use existing routes/send/unregister operations.

### Verified run receipts and handovers (#141)

`run --label TEXT [--cwd DIR] [--state-dir DIR] -- COMMAND...` captures a local
command without contacting the broker. `<state-dir>/receipts/<id>.json` records
`schema: agent-bridge-run/1`, `id`, `label`, `argv`, `cwd`, UTC `started`/`finished`,
`exit_code`, sibling `log`, `log_sha256`, `log_bytes`, `capture`, `log_order`, `head` and `dirty`.
Git HEAD/dirty are sampled before the command; unavailable metadata is null, never
inferred clean. This does not freeze the source tree. The command's exit status
is returned (signals use 128 + signal), and the last stderr line is
`RUN_RECEIPT: <id>`. No shell expansion is applied; use `sh -c` explicitly if needed.

All TTY/redirection combinations preserve each child's stdio `isatty()` state.
Stdin is inherited unchanged in every layout, including full TTY and piped input.
The recorder does not read ahead, flush queued input or change original terminal
settings. With TTY stdin the child retains the caller's session/process group;
terminal SIGINT reaches it through that group and is not forwarded a second time.
With TTY stdin, `kill -INT <recorder_pid>` targeting only the recorder is
suppressed and does not interrupt the child; use terminal/group SIGINT instead.
Each terminal output uses an output-only PTY with window-size propagation;
outputs on the same terminal share one PTY. Each redirected output uses its own
pipe and is teed to its original stdout or stderr destination. The intermediate
PTY disables `OPOST` so the destination terminal applies line processing once.
This supports ordinary `printf 'answer\n' | ...` from a terminal, and independent
`>out 2>err` redirections.

Receipts record `capture: routed` and `log_order`. Strict combined write order is
guaranteed only for shared terminal output (`log_order: shared-pty`). For distinct
pipes/terminals, the log records observed relay read order (`observed-read-order`);
original global cross-stream write order cannot generally be recovered. Each
stream retains its own byte order and fd routing. The log contains captured
stdout/stderr only: input echo on the original terminal and explicit `/dev/tty`
writes bypass capture. This foreground recorder does not implement full shell
job control or record stdin independently. Output PTY log bytes are captured
before destination-terminal line processing; child-emitted ANSI/CRLF are not
stripped or normalized. A missing final stderr newline is added before the
receipt marker so it starts on the last stderr line.

Receipt/log files are 0600 under a 0700 receipts directory. Writers use
`O_NOFOLLOW`, exclusive locks, `fchmod`, and file/directory `fsync` as retros do;
readers use shared locks and reject symlinks/nonregular files. JSON is published
only after capture finishes and the log is synced. Capture failure may leave an
orphan log but no completed receipt. There is no automatic expiry: the owner
manages these files privately. Logs can contain sensitive command output.

A reference is a bridge id or a JSON path. Bridge logs are checked against their
recorded byte count and SHA-256. The Nova pool shape reads integer `exitCode`,
optional `sourceCommit` (full SHA or empty/absent = unknown), and **sibling
`command.log`** beside `result.json`; remote `root`/`out` are not local log paths.
A generic external JSON may instead supply integer `exit_code`, `log` (relative
to that JSON or absolute), and optional `head`. External receipts have no required
log hash. None of these formats authenticates provenance: the owner can edit
files, and matching figures does not establish that a claim is meaningful.

`handover check --rows FILE --head FULL_SHA [--state-dir DIR]` verifies locally,
without a broker. Rows are a nonempty JSON list of
`{claim, receipt, exit, figures:["literal text"]}`. An optional row `head` explicitly
states another full SHA. Missing/unreadable receipts/logs, invalid shapes, exit
mismatches, missing literal UTF-8 figures, known-head mismatches and native log
hash/size mismatches refuse as `HANDOVER_UNVERIFIED: row N: ...` (one-based).
Unknown recorded heads remain visibly unknown. No tests are chosen or run.

`handover --project P --from RUNNER --to TOWER --card REF --head FULL_SHA --rows FILE
[--note-file FILE] [--state-dir DIR]` checks every row before sending one existing
broker message. Claim/figure/reference cells are JSON-quoted, preserving text and
escaping table delimiters. Each row shows exit, verification scope, receipt kind,
reference and stated/recorded HEAD; earlier/different heads are marked explicitly.
The optional note follows verbatim under an **unverified free-text** separator.
Only references and stated row content are sent, never receipt JSON or full logs.
The message must fit 32768 UTF-8 bytes. Existing explicit routes are required;
identical content has a deterministic broker idempotency key. Never retry an
ambiguous dispatch. Delivery acceptance is not completion; no restart is needed.

Worked example (replace `FULL_CURRENT_SHA` with the actual full commit SHA and
`RECEIPT_ID` with the id printed by `run`; choose tests yourself):

```bash
bridge_cli="$HOME/.agents/skills/agent-bridge/scripts/bridge.py"
lane="$HOME/.local/share/agent-bridge/bridge.state/example-141"
mkdir -p "$lane"
# In an ordinary terminal, all three stdio descriptors are TTYs.
python3 "$bridge_cli" run --label stdin-smoke -- python3 -c \
  'import sys; value=input("Value: "); print("INPUT="+value); print("checks=1")'
# Enter probe-141. Copy the printed receipt id into this private rows file.
cat > "$lane/rows.json" <<'JSON'
[{"claim":"stdin smoke","receipt":"RECEIPT_ID","exit":0,"figures":["INPUT=probe-141","checks=1"]}]
JSON
python3 "$bridge_cli" handover check --head FULL_CURRENT_SHA --rows "$lane/rows.json"
python3 "$bridge_cli" handover --project P --from runner --to tower --card repo#141 \
  --head FULL_CURRENT_SHA --rows "$lane/rows.json"
# Noninteractive input from the same terminal keeps child stdio False,True,True:
printf 'probe-141\n' | python3 "$bridge_cli" run --label pipe-smoke -- python3 -c \
  'import os,sys; print([os.isatty(i) for i in range(3)]); x=input(); print("INPUT="+x); sys.exit(7)'
# Independent destinations stay separate; the log promises observed read order:
printf 'probe-141\n' | python3 "$bridge_cli" run --label routed-smoke -- python3 -c \
  'import sys; print("OUT="+input()); print("ERR",file=sys.stderr)' >"$lane/out" 2>"$lane/err"
# A retrieved pool result can replace the receipt id with an absolute path to
# result.json; keep its sibling command.log. An earlier run needs an explicit
# row "head" when its sourceCommit differs. Empty sourceCommit stays unknown.
```
