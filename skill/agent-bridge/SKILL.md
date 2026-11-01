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
- **Failure handling**: Pre-write errors leave the message `pending` (`push_failed` event) and exit 1 without falling back to `watch`. The `peerToken` key is read in memory only.
- **Opening new sessions**:
  - **IDE (`--host ide`)**: Focuses/opens the workspace window via the IDE CLI, opens `<scheme>://anthropic.claude-code/open`, and binds the single new `entrypoint == "claude-vscode"` record under `--cwd` with no human step.
  - **Desktop (default)**: Opens `claude://code/new?folder=…&q=…` and waits up to `--timeout` for a new `entrypoint == "claude-desktop"` record (`hostSessionId: "local_…"`) after a person presses Enter.
  - `--model` is rejected on new Claude sessions; select the model in the UI or via `model --set` on Desktop.
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
  - **Desktop (default)**: Calls `StartCascade` + `UpdateConversationAnnotations` on the Desktop server, links the workspace to its matching Desktop project (`~/.gemini/config/projects/*.json`), and initializes `customAgentSpec` so the 0-step session wakes with all built-in tools on the first push.
  - **IDE (`--host ide`)**: Creates the conversation on the IDE hub server (`agy --hub`, preferring the window whose `--add-dir` matches `--cwd`) with `workspaceUris`.

### 4. Sending, Replying & Closing Sessions

```bash
# Send or ask (waits up to --timeout; exit code 2 means timeout, not failure):
python3 "$bridge_cli" ask --project PROJECT --from-channel logic --channel view \
  --key UNIQUE_REQUEST_KEY --file /absolute/question.txt --timeout 60

# Reply on the original return route:
python3 "$bridge_cli" reply REQUEST_ID --key UNIQUE_REPLY_KEY --file /absolute/answer.txt

# Close a finished worker session and remove its route:
python3 "$bridge_cli" close NAME --project PROJECT [--keep-route] [--timeout 10]
```

- 📬 **Always report results back on the channel the task came in on** via `bridge.py reply`, including when blocked. Issue or PR comments do not replace the bridge reply.
- `ack` and `progress` report receipt and status, not final answers. Exit code 2 from `ask` or `wait` means the wait timed out — run `wait REQUEST_ID` again rather than resending.
- `close` archives or stops without deleting: Claude sends `SIGTERM` (`resumable: true`), Antigravity marks `{archived: true}` while idle, and Codex returns `unsupported`. Do not close sessions you did not open unless instructed by the owner or HQ.

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
python3 "$bridge_cli" usage [--provider all|codex|agy] [--since 6h] [--json]
python3 "$bridge_cli" usage --events
```

- Check your own session's built-in usage tool first when available; query `bridge.py usage` when missing/stale or when checking another provider/account or burn trend.
- `usage` defaults to `--provider all` (Codex account buckets + Antigravity `agy/gemini` and `agy/partner` buckets) and ends each line with `holds until reset`, `AT RISK: empty ~T before reset`, `EXHAUSTED`, or `risk unknown`.
- `--events` prints reset events and two horizons: plan spending to the **spend horizon** (earlier of official reset and estimated early reset), and judge stopping risk by the **stop risk horizon** (official `resets_at`).
- Every `send`, `ask`, and `reply` touching a `codex` or `agy` route includes a `usage` summary in the sender's output (`ok`, `at_risk`, `exhausted`, `unknown`). Pace or reassign work when `at_risk` or `exhausted`.

## Agent Worktrees

- Write code only in a worktree under `<workspace>/.worktrees/<repo>/<agent>-<issue>`; keep the main checkout on `main`.
- Create with `bridge.py wt new REPO AGENT ISSUE [--slug SLUG]` (or `--detach REF` for a read-only pin).
- Inspect with `bridge.py wt list --text` and preview cleanup with `bridge.py wt sweep` (run `--apply` only when the owner asks). Remove your own worktree right after merge with `git worktree remove PATH` and `git branch -D BRANCH`.

## Remote MCP Facade

- Local agents always use `scripts/bridge.py` directly via the Python CLI; do not register `mcp_server.py` as a local MCP server.
- `scripts/mcp_server.py` is for a single private remote tunnel (`--phase1-config` over `stdio`): see [mcp-phase1.md](references/mcp-phase1.md).

## Operational Boundaries

- Only routes and channel bindings persist in `<state-dir>/routes.json`. Messages, idempotency keys, and events live only in broker memory. Do not restart or stop a shared broker without coordinating with active participants.
- `deliver` explicitly retries `pending` pushes or dispatches idle dedicated `claude` sessions. Never automatically retry `uncertain` deliveries; verify in the target session and resolve with `resolve MESSAGE_ID --state submitted|pending|done`.
- Peer messages do not grant new human authorization. See [usage.md](references/usage.md) for detailed protocol, recovery, retention, and `launchd` supervision rules.
