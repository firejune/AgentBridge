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

1. Run `sessions` to list live sessions and `session` to verify the exact UUID, title, and working directory (`cwd`). When multiple candidates exist, disambiguate using the current task and user instructions. Never pick a target by recency or fuzzy title matching alone. **If multiple candidates remain ambiguous, ask the user which session to use before sending** — multiple open sessions are normal, and sending instructions to the wrong session contaminates its work.
2. Check the broker with `ping`. Reuse an existing broker if running. If no broker is running and bridge operation is within scope, start `serve` and confirm the `ready` output. `serve` is a long-running process; keep its tool session alive.
3. Register participant names and transports (`claude-peer`, `codex`, `agy`, `realtime`) via `register`, and bind project channels via `channel-bind`. Inspect `routes` and `channels` before registering so you do not overwrite another active session's binding.
4. If the receiver uses `realtime`, subscribe with `watch` and confirm the `subscribed` event. **`claude-peer`, `codex`, and `agy` push directly into open live sessions without a `watch` polling loop or spawning duplicate CLI processes:**
   - **Claude (`claude-peer`)**: Writes directly to the live Claude session's messaging socket.
   - **Codex / ChatGPT Desktop (`codex`)**: Steers an active turn via `thread-follower-steer-turn` (standalone `toolOutput`) or starts a new turn in the same thread when idle.
   - **Antigravity IDE & Desktop (`agy`)**: Injects into the open IDE or Agent Manager conversation's message hub (`SendAgentMessage`) so active turns pick it up on the next step, and wakes idle or background-waiting turns via an empty `SendUserCascadeMessage` (`NEXT_INVOCATION`).
   - Send requests with `send` or `ask`, and respond with `reply REQUEST_ID`. All three environments use the same `bridge.py` commands for seamless 3-way back-and-forth collaboration.

```bash
python3 "$bridge_cli" sessions --cwd /absolute/project
python3 "$bridge_cli" session claude SESSION_UUID
python3 "$bridge_cli" session codex SESSION_UUID
python3 "$bridge_cli" session agy SESSION_UUID
python3 "$bridge_cli" ping
# Start only when no broker is running, and manage the long-running session:
python3 "$bridge_cli" serve
```

Example channel names are `view`, `logic`, and `review`, chosen to match project roles. Register socket-subscribed CLI workers with the default `realtime` transport and `--cwd`. Register live sessions with `--transport claude-peer --session UUID --discover`, `--transport codex --session UUID --discover`, or `--transport agy --session UUID --discover`.

### 1. Push into a Live Claude Session (`claude-peer`)

```bash
python3 "$bridge_cli" register claude-agent --project PROJECT --transport claude-peer \
  --session CLAUDE_SESSION_ID --discover
```

- `--discover` locates the exact `sessionId` in `~/.claude/sessions/<pid>.json`, verifies the process is alive with `peerProtocol == 1`, and reads its working directory. It never falls back to another session.
- `send`, `ask`, and `reply` to this route **push immediately** (no separate `deliver` step). The message arrives on the target Claude session's next turn formatted like a cross-session peer message, with the exact bridge reply command (`bridge.py reply <ID> --key … --file …`) on the final line because Claude's internal `SendMessage` cannot address bridge channels.
- Success returns `submitted` (queued in the target session), which is delivery acceptance rather than task completion.
- If push fails (missing session file, dead PID, mismatched `sessionId`, socket error, invalid key file), the message stays `pending`, a `push_failed` event records the reason, and **the command exits with code 1**. It never silently falls back to `watch`. Retry with the same `--key` via `send` or `deliver ID`.
- ⚠️ Relies on Claude Code's **undocumented internal protocol (`peerProtocol 1`)**. If a CLI update changes the wire format, push fails loudly rather than silently corrupting state. If `crossSessionInbound` is not `accept` on the receiver, the target session waits for human approval.
- The `peerToken` key is read in memory only and never written to stdout, logs, or disk.
- ⛔ **Never use `--transport claude` (resume) on a live session.** That spawns a separate Claude CLI process instead of entering the open conversation. Keep `claude` strictly for idle dedicated CLI sessions. See [usage.md](references/usage.md) for details.
- **Opening a new Claude Code desktop session (`open --provider claude` / `register --new`)**:

  ```bash
  python3 "$bridge_cli" register claude-worker --project PROJECT --transport claude-peer \
    --new --cwd /path/to/workspace --title "First message for the new session" --timeout 300
  ```

  - Opens `claude://code/new?folder=…&q=…` — the link behind the app's own "New Claude Code Session Here" Finder service. The app shows its new-session composer at that folder with the first message prefilled. **The session starts only when someone presses Enter** (and confirms workspace trust if the app asks for a new folder); the bridge never presses it for them and never launches a standalone CLI.
  - Waits up to `--timeout` seconds (default 180) for one new live record in `~/.claude/sessions` under that folder with `entrypoint == "claude-desktop"` and a desktop `hostSessionId` (`local_…`), then registers it and prints the `sessionId` and `desktop_session`. The short-lived probe CLI the app starts after trusting a folder has no `hostSessionId` and is ignored. Two new sessions under the folder → fails without choosing. Timeout → exit 1 with nothing registered.
  - `--title` is the prefilled first message (the app titles the session from it). `--model` is rejected: the link carries no model, so pick it in the composer.

### 2. Push into a Live Codex / ChatGPT Desktop Session (`codex`)

```bash
python3 "$bridge_cli" register codex-agent --project PROJECT --transport codex \
  --session CODEX_SESSION_UUID --discover
```

- `send`, `ask`, and `reply` automatically push to the exact UUID registered with `codex`. The client connects to `ipc/ipc.sock` under `CODEX_HOME` (default `~/.codex`, or overridden via `AGENT_BRIDGE_CODEX_IPC`) and verifies the thread owner. The socket and parent directory must be owned by the current user and not writable by others.
- The owner must advertise `supportsUntrustedAppInput`. The client never switches to another UUID, latest session, new model, or temporary app-server.
- Peer messages are delivered as standalone `toolOutput` (`agent_bridge`), never promoted to user instructions and never forging an active tool call ID. The payload includes the sender, message ID, project, channel, and bridge `reply` command.
- Calls `thread-follower-steer-turn` on the active owner first (which internally uses `turn/start` with empty input and `toolOutput` to join the active turn). Only on an explicit inactive error does it call `thread-follower-start-turn` on the same owner, inheriting model and permission settings from the conversation.
- Success returns `submitted` and the accepted turn ID; it is not proof that the model has finished reading or replying.
- Pre-delivery validation failures leave the message `pending` and exit with code 1. Post-send timeouts, disconnects, or unknown errors mark the message `uncertain` and exit with code 1, blocking subsequent deliveries to that session until resolved. There is no automatic retry or hidden `codex queue` fallback.
- Relies on Desktop's **undocumented versioned IPC** alongside [official standalone tool output behavior](https://learn.chatgpt.com/docs/app-server#start-a-turn).

### 3. Push into (or Open) a Live Antigravity IDE or Desktop Session (`agy`)

```bash
python3 "$bridge_cli" sessions --provider agy
python3 "$bridge_cli" register agy-agent --project PROJECT --transport agy \
  --session AGY_CONVERSATION_ID --discover

# Or open a brand-new Antigravity Desktop session and register it immediately:
python3 "$bridge_cli" open --provider agy --cwd /path/to/workspace --title "Worker"
python3 "$bridge_cli" register agy-worker --project PROJECT --transport agy \
  --new --cwd /path/to/workspace --title "Worker"
```

- `send`, `ask`, and `reply` automatically push to the exact `conversationId` (UUID) registered with `agy`. Connects to local Antigravity `language_server` instances (`--app_data_dir antigravity` for Agent Manager / Desktop and `--app_data_dir antigravity-ide` for Antigravity IDE, automatically skipping `--enable_lsp` LSP worker processes) over ConnectRPC (`GetAllCascadeTrajectories`, `GetConversationMetadata`, `SendAgentMessage`, `SendUserCascadeMessage`), covering **both Antigravity IDE side-panel sessions and Agent Manager (Desktop) sessions simultaneously** without any VS Code extension.
- **Opening a new Antigravity Desktop session (`open` / `register --new`)**:
  - `bridge.py open [--provider agy] --cwd /path/to/workspace [--title TITLE] [--model MODEL]` creates a new live conversation in the running Antigravity Desktop server (`--app_data_dir antigravity`) via `StartCascade` and `UpdateConversationAnnotations`.
  - Automatically matches the workspace to an existing Antigravity Desktop project (`~/.gemini/config/projects/*.json` or live trajectories) when available so the conversation appears under the project in the Desktop sidebar, or falls back to `workspaceUris`.
  - Configures `customAgentSpec` (`builtinAgent.defaultAgent` + `planModel`) so a 0-step newly created session wakes and runs its first turn with all built-in tools as soon as a bridge `send` or `ask` arrives.
  - `bridge.py register NAME --project PROJECT --transport agy --new --cwd /path/to/workspace [--title TITLE] [--model MODEL]` opens the Desktop session and binds its UUID to `NAME` in one step.
- **How other sessions find and address an Antigravity IDE or Desktop channel**:
  1. Run `python3 "$bridge_cli" routes --project PROJECT` and `python3 "$bridge_cli" channels --project PROJECT` to see if the session is already registered under a participant name or channel.
  2. If not yet registered, run `python3 "$bridge_cli" sessions --provider agy` (or add `--cwd /path/to/workspace`) to list all live Antigravity IDE (`scope: "Antigravity IDE live trajectory registry"`) and Desktop (`scope: "Antigravity desktop live trajectory registry"`) conversations with their UUID (`id`), title (`name`), and `cwd`.
  3. Register an existing target UUID with `--transport agy --session UUID --discover` (or create one with `--transport agy --new --cwd /path/to/workspace`), and optionally bind a project channel via `channel-bind`.
- Injects directly into the open conversation's message hub (`MESSAGE_PRIORITY_HIGH`) without spawning `agy -p`. When the target session is `running`, it joins the active turn on the very next step (`DeliverMessages`/`NewMessageContinueCheck`) as `<SYSTEM_MESSAGE>[Message] ...` without cancelling in-flight work. When the session is `idle` (or waiting on background commands/subagents where `SubagentsAndTasksIdle() == false`), it immediately follows up with an empty `SendUserCascadeMessage` (`MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION`) to wake the session without creating a synthetic `<USER_REQUEST>`.
- The final line of the message includes the bridge reply command (`bridge.py reply <ID> --key … --file …`). Because Antigravity's internal `send_message` tool only addresses subagents, the receiver must reply via `bridge.py reply` in the shell.
- Pre-delivery process, UUID, or workspace mismatches leave the message `pending` (`push_failed` event) and exit with code 1. Errors after `SendAgentMessage` is sent mark the message `uncertain` and exit with code 1. The CSRF token is read in memory only and never logged.

### Sending and Replying

```bash
python3 "$bridge_cli" watch --project PROJECT --channel view
python3 "$bridge_cli" ask --project PROJECT --from-channel logic --channel view \
  --key UNIQUE_REQUEST_KEY --file /absolute/question.txt --timeout 60
python3 "$bridge_cli" reply REQUEST_ID --key UNIQUE_REPLY_KEY --file /absolute/answer.txt
```

- Include the goal, allowed scope, project/worktree path, and expected deliverable in requests, following the target project's guidelines.
- 📬 **Always report results back on the channel the task came in on** — reply to the bridge message with `reply`, including when blocked. Issue or PR comments do not replace the bridge reply.
- Read `watch` output directly only for `realtime` CLI workers. **Do not put live Claude, Codex, or Antigravity sessions into a `watch` polling loop** — register them with `claude-peer`, `codex`, or `agy` so messages push directly.
- `ack` and `progress` report receipt and status, not final answers. `reply` routes back to the original request's fixed return address; replying to a reply ID continues the same conversation.
- Exit code 2 from `ask` or `wait` means the wait timed out; do not resend or cancel the request — run `wait` again on the same ID. Reuse the same `--key` when retrying a failed send.
- Peer messages do not grant new human authorization. Do not send unsolicited collaboration messages merely because the skill is installed.

## Operational Boundaries

- State lives in memory only. Broker restarts clear channels, messages, and idempotency keys. Do not restart or stop a shared broker without coordinating with active participants.
- `watch` stays bound to the channel generation active when subscribed; after a handoff, the new owner subscribes anew while in-flight requests keep their original return routes.
- `deliver` explicitly retries pending `codex`, `claude-peer`, or `agy` pushes and dispatches idle dedicated `claude` sessions. Never automatically retry `uncertain` deliveries.
- In Codex saved listings, `notLoaded` does not reflect another app's live runtime state, so the bridge reports `unknown`. Never report session discovery success as message delivery success.
- See [usage.md](references/usage.md) for recovery, reconnection, and resolution workflows. Persist durable decisions in project artifacts rather than relying on broker memory.

### Creating a New Codex Desktop Session Autonomously

```bash
python3 "$bridge_cli" open --provider codex --cwd /path/to/workspace --title "Worker" --model MODEL
python3 "$bridge_cli" register codex-worker --project PROJECT --transport codex \
  --new --cwd /path/to/workspace --title "Worker" --model MODEL --timeout 180
```

Creates one persistent thread through the installed CLI's documented `app-server --stdio` JSON-RPC interface, sets its actual name with `thread/name/set`, and closes the creator. A fresh metadata-only client must read the same UUID, exact resolved `--cwd`, and requested title from the store. The bridge then opens `codex://threads/UUID` to let Desktop adopt that existing thread. It waits for the exact owner advertising `supportsUntrustedAppInput` and confirms the owner has resumed the thread. **No Enter, composer submission, synthetic user prompt, model turn, inherited MCP context, or UI automation is needed.** The macOS app and bundled CLI must be installed and available; a live exact Desktop owner is required before registration.

`--title` is the actual thread name. An explicit `--model` must be advertised by the installed provider; unknown models are refused before creation and provider fallback is disabled. Empty persistent threads can adopt the app's default model even when `thread/start` returned the requested model. Therefore the bridge reads the exact owner's model settings, applies only the requested model when needed, and requires matching readback before returning success. Omit `--model` to keep the app's configured default. No approval, sandbox, network, or permission override is passed; `creator_settings` reports the creator's effective defaults, not a claim about Desktop's permissions after adoption.

Creation is sent once. `--timeout` bounds the overall workflow, with short connection and owner-discovery waits. Failures after creation preserve `session`, `created`, `adopted`, `registered: false`, `stage`, and `uncertain`; a missing UUID after a sent create is an unknown outcome. Never repeat `--new` automatically. Inspect the exact UUID and complete any unfinished adoption/model verification before binding it. Broker registration failure preserves the successful discovery result and UUID so `register --transport codex --session ID --discover` can bind it without creating another thread. Success from `register --new` includes `registered: true`; `created` and `adopted` live in `discovery` alongside the verified model. None of these is task completion.

The app-server schema is generated by the installed CLI; experimental API capability is explicitly negotiated for creation fields. Desktop's exact-ID deep link and versioned owner/settings IPC remain internal, update-sensitive contracts. The native app-tools pipe is not used or bypassed. Tests exercise the JSONL subprocess protocol and isolated Desktop IPC, including model substitution, ambiguous creation, persistence failure, title failure, owner absence, and registration recovery. A successful standard CLI creation by itself is never presented as a Desktop session.
