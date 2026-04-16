# AgentBridge

AI-to-AI autonomous communication bridge between IDE-based agents and external CLI agents.

AgentBridge enables **bidirectional, zero-human-intervention messaging** between an IDE agent (e.g., [Antigravity](https://blog.google/technology/google-deepmind/antigravity/)) and a CLI agent (e.g., [OpenClaw](https://github.com/nicepkg/openclaw)), turning multiple AI assistants into a cooperative team.

## How It Works

```
[CLI Agent] ── send-to-agent.sh ──→ [IDE Agent]
    │        (routing header)            │
    │                                 performs task
    │                                    │
    │  ←── reply-from-agent.sh ──── [IDE Agent]
    │
    └── CLI Agent receives result
```

1. **CLI Agent sends a task** — The shell script injects a message into the IDE agent via a local HTTP bridge (VS Code extension) pushing directly to the agent's internal API (`antigravity.sendPromptToAgentPanel`).
2. **IDE Agent works autonomously** — Receives the prompt silently in the background with a routing header, performs the requested task without stealing user focus.
3. **IDE Agent replies** — Sends the result back to the CLI agent's session

## Components

### VS Code Extension (`src/extension.ts`)

A lightweight HTTP server (port `18880`) running inside the IDE that provides:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check with version (1.2.0) |
| `/status` | GET | Last injection result |
| `/trigger` | POST | Call IDE API (`antigravity.sendPromptToAgentPanel`) to send prompt directly |
| `/debug/commands` | GET | List registered IDE commands (useful for discovery) |

### Shell Scripts (`scripts/`)

| Script | Direction | Description |
|--------|-----------|-------------|
| `send-to-agent.sh` | CLI → IDE | Sends a message with routing header to the IDE agent |
| `reply-from-agent.sh` | IDE → CLI | Sends a reply back to the CLI agent's session |

### Routing Header

Messages include a machine-readable header for session tracking:

```
[CHLOE_BRIDGE]
session_id=<uuid>
request_id=<uuid>
[/CHLOE_BRIDGE]

Actual task instructions here
```

## Quick Start

### 1. Install the Extension

```bash
# Use the IDE's own CLI to avoid mis-installation to other editors
/path/to/your-ide --install-extension luna-antigravity-bridge-1.2.0.vsix --force
```

Then reload the IDE window (`Cmd + R`).

### 2. Send a Message (CLI → IDE)

```bash
./scripts/send-to-agent.sh 'Check system status please.' \
  --session-id '<your-session-uuid>'
```

### 3. Reply (IDE → CLI)

```bash
./scripts/reply-from-agent.sh \
  --session-id '<session-uuid>' \
  --message 'All services are healthy.'
```

### 4. Dry Run

Preview the assembled message without sending:

```bash
./scripts/send-to-agent.sh 'test message' \
  --session-id '<uuid>' \
  --dry-run
```

## Prerequisites

- **macOS** — Uses `osascript` for app activation and menu-based paste
- **Accessibility permission** — Required for System Events control
- **IDE must be running** — The extension HTTP server is only active while the IDE is open
- **Run from external terminal** — Antigravity's built-in terminal is sandboxed; use Terminal.app or iTerm2

## Architecture Decisions

This project went through 7 iterations to find a stable approach:

| Attempt | Approach | Result |
|---------|----------|--------|
| 1 | Pure AppleScript | Failed — sandbox error (1002) |
| 2 | Extension + Node.js exec | Failed — UI event lock conflict |
| 3 | Virtual Document + API | Partial — no auto-submit |
| 4 | VSIX packaging fixes | Root cause found for IDE mis-install |
| 5 | Chat API direct injection | Failed — standard `chat` commands not registered |
| 6 | Hybrid: Extension (clipboard) + osascript | Succeeded but Flaky — stealing focus, UI flakes |
| 7 | **Ideal: Extension direct internal API call** | **Success** — Silent, background execution via `antigravity.sendPromptToAgentPanel` without UI automation |

The key insight: By digging into the proprietary internal command palette, we discovered the `antigravity.sendPromptToAgentPanel` command, which allows completely bypassing brittle UI automation for a perfectly silent, native bridge experience.

## Parameters Reference

### `send-to-agent.sh`

| Parameter | Required | Description |
|-----------|----------|-------------|
| message (positional) | ✅ | Task instructions for the IDE agent |
| `--session-id` | ✅ | CLI agent's session UUID for reply routing |
| `--request-id` | ❌ | Request tracking ID (auto-generated if omitted) |
| `--dry-run` | ❌ | Preview assembled message without sending |

### `reply-from-agent.sh`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `--session-id` | ✅ | Target CLI agent session UUID |
| `--message` / `-m` | ✅ | Reply content |
| `--request-id` | ❌ | Original request ID for tracking |
| `--dry-run` | ❌ | Preview without sending |

## Shell Quoting Rule

Always use **single quotes** for parameter values. Double quotes with `!` trigger bash history expansion:

```bash
# ✅ Correct
--message 'Task completed!' --session-id '5671c52b-...'

# ❌ Broken (!" triggers history expansion)
--message "Task completed!" --session-id "5671c52b-..."
```

## Best Practices

### File-Based Reporting

For detailed reports, write a markdown file and send only the file path as a single-line message. The CLI agent can then read the file for full context.

```bash
# Step 1: IDE agent writes report to a shared location
# e.g., .openclaw/reports/<request-id>-<task-name>.md

# Step 2: Send file path as reply
./scripts/reply-from-agent.sh \
  --session-id '<uuid>' \
  --message 'Task complete. Report: .openclaw/reports/abc123-deploy.md'
```

This avoids message formatting issues and keeps a persistent record of all reports.

### Keep Reply Messages Short

The `reply-from-agent.sh` `-m` parameter works best with **single-line messages**. Multi-line content may cause rendering issues in some CLI agent UIs.

## Documentation

- [`docs/ide-agent-side-setup.md`](docs/ide-agent-side-setup.md) — IDE agent (receiver) setup guide
- [`docs/openclaw-side-setup.md`](docs/openclaw-side-setup.md) — CLI agent (sender) setup guide

## License

MIT
