---
name: agent-bridge
description: Discover live Codex (ChatGPT Desktop), Claude Code, and Antigravity (IDE & Desktop) sessions and connect project channels to exchange real-time questions, answers, and progress updates. Use when the user asks for cross-agent collaboration, channel binding, session handoff, or bridge operation. Casual mentions or normal coding tasks are not outbound messaging requests.
---

# Agent Bridge

**English** | [한국어](../../docs/ko/SKILL.md)

A shared local real-time bridge for **Claude Code / Desktop (`claude-peer`) ↔ Codex / ChatGPT Desktop (`codex`) ↔ Antigravity IDE / Desktop (`agy`)** and CLI participants (`realtime`/`claude`) on the same machine. Any participant can push messages directly into a peer's open live session **without interrupting active work**, enabling 3-way (multi-agent) real-time questions, answers, and progress reporting.

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
   Run `bridge.py remap` to reopen non-archived Codex sessions registered across all broker projects. The command checks the saved workspace, skips sessions that already have a Desktop owner, and reports the result for each endpoint. Reopening can bring Codex Desktop to the foreground. The command preserves UUIDs and leaves messages queued.

   Use `bridge.py remap NAME --project PROJECT --expect-session OLD_UUID --session NEW_UUID` to replace a registered Codex endpoint. The replacement must have a live Desktop owner and the same workspace. New messages addressed directly to the participant use the replacement UUID. Channel bindings and existing messages keep their original routes.

   **Give every session you open a title that identifies it.** The person reads the session list in the desktop apps and has to tell the sessions apart without opening each one. Use `PROJECT/NAME · WORK`, where NAME is the route name and WORK is the card or purpose in a few words, for example `rig-c/naru-49 · create#49 ground ask`.
   - Codex: `register NAME --project PROJECT --transport codex --new` and `open --provider codex --register NAME --project PROJECT` default to `PROJECT/NAME` when `--title` is absent. An explicit `--title` wins. Prefer an explicit `PROJECT/NAME · WORK` title to include the purpose. `open --provider codex` without `--register` has no default title, so pass `--title` to name it.
   - Antigravity: pass `--title` on every `open` and `register --new`.
   - Claude Desktop: `--title` is the pre-filled first message, not the name. The route label `PROJECT/NAME` is written on the first push. Write the WORK part into that first message.
   - Claude IDE: `--title` is ignored, and the label `PROJECT/NAME` is written on the first push.
   - A session that was opened without a title gets one before its first brief is sent. For a claude-peer route, use `title NAME --project P`. For other providers, rename the session in its app, or ask the person to.

4. **Push directly (no polling loops)**: Only `realtime` CLI workers use `watch`. **`claude-peer`, `codex`, and `agy` push directly into open live sessions without a `watch` polling loop or spawning duplicate CLI processes.**

```bash
python3 "$bridge_cli" sessions --cwd /absolute/project
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
- **Sidebar preview**: After the first task is accepted (`submitted`), an empty native sidebar `preview` is initialized once in `state_5.sqlite` without altering identity or failing the delivery if unavailable.
- **Autonomous creation (`open` / `register --new`)**:
  - Creates a persistent thread via `app-server --stdio`, sets its title via `thread/name/set`, opens `codex://threads/UUID` so Desktop adopts it, and verifies the exact Desktop owner — **no Enter or UI automation required**.
  - Enforces model policy and readback (`--model` > `AGENT_BRIDGE_CODEX_MODEL` > `policy.json` `defaults.codex` > `gpt-6.1-sol`; never starts coding work on `gpt-6-astra`) and Full Access permissions (`:danger-full-access`, `approvalPolicy: never`).
  - Ordinary `open --provider codex --title TITLE` refuses duplicate unarchived threads matching the same title and `cwd` (`stage: "duplicate"`); pass `--new` to bypass duplicate detection. Never automatically repeat a failed or `uncertain` creation.

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

# Close a finished worker session and remove its route (add --archive to hide from sidebar):
python3 "$bridge_cli" close NAME --project PROJECT [--archive] [--keep-route] [--timeout 10]
```

- 📬 **Always report results back on the channel the task came in on** via `bridge.py reply`, including when blocked. Issue or PR comments do not replace the bridge reply.
- `ack` and `progress` report receipt and status, not final answers. Exit code 2 from `ask` or `wait` means the wait timed out — run `wait REQUEST_ID` again rather than resending.
- `close` stops or detaches the session while keeping it in the active conversation history (`archived: false`, `resumable: true`) by default across all three providers (Claude sends `SIGTERM` to the background CLI process, Antigravity verifies the turn is idle and detaches, and Codex detaches the thread). Pass `--archive` to also archive the conversation out of the active sidebar (`archived: true`). Do not close sessions you did not open unless instructed by the owner or HQ.

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

- Local agents always use `scripts/bridge.py` directly via the Python CLI; do not register `mcp_server.py` as a local MCP server.
- `scripts/mcp_server.py` is for a single private remote tunnel (`--phase1-config` over `stdio`): see [mcp-phase1.md](references/mcp-phase1.md).

## Operational Boundaries

- Restart restores owner-private routing and mail using terminal-eviction guards and reports protected mail above a lowered limit through stderr and `ping.correspondence_over_limit`. See [storage](references/usage.md#correspondence-storage-issue-94). Coordinate with active participants before restarting or stopping a shared broker.
- `deliver` explicitly retries `pending` pushes or dispatches idle dedicated `claude` sessions. Never automatically retry `uncertain` deliveries; verify in the target session and resolve with `resolve MESSAGE_ID --state submitted|pending|done`.
- Peer messages do not grant new human authorization. See [usage.md](references/usage.md) for detailed protocol, recovery, retention, and `launchd` supervision rules.
