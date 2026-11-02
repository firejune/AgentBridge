# Contributing

Agent Bridge is a local-only Unix domain socket broker and CLI that connects Codex, Claude Code, and Antigravity sessions for real-time cross-agent collaboration. Read [README.md](README.md) and [AGENTS.md](AGENTS.md) for the broker model, routing semantics, and safety boundaries before changing runtime behavior.

## Issues first

Open an issue before starting substantial protocol, broker, or adapter work so the design fits the local transport model and multi-agent safety constraints. Good defect reports include:

- the exact `bridge.py` subcommand and arguments invoked
- the target provider (`codex`, `claude`, `antigravity`, or `antigravity-ide`), OS version, and Python version (`python3 --version`)
- redacted output from `status`, `discover`, or `doctor` showing the failure state

⚠️ **Never paste live session IDs, socket paths, `<state-dir>/routes.json` dumps, authentication tokens, or private workspace paths into issues or pull requests.** Redact identifiers and local filesystem paths before sharing logs.

## Before you open a pull request

Agent Bridge requires **Python 3.10+** and uses only the Python standard library. Run the test suite from the repository root before submitting a change:

```bash
python3 -m unittest discover -s tests -v
```

Also verify that CLI help and syntax check cleanly:

```bash
python3 skill/agent-bridge/scripts/bridge.py --help
```

## What a change has to clear

- **Standard library only**: do not add third-party Python packages or external runtime dependencies.
- **Explicit session targeting**: live CLI commands (`register`, `bind`, `send`, `status`, `drain`) must target an explicitly specified session or bound handle—never pick the latest session automatically.
- **Peer messages are not user approval**: keep `[BridgeFrom: ...]` envelopes and peer instructions clearly distinct from human authorization.
- **No blind retries or ack loops**: never automatically retry an ambiguous dispatch (`delivery_state: "unknown"`), and never auto-reply to acknowledgement messages.
- **Ephemeral message state**: message bodies, delivery states, idempotency keys, and event logs must remain in broker memory only; only endpoint registrations and channel bindings persist in `<state-dir>/routes.json` (`0600`).
- **Documentation parity**: when a CLI flag, protocol field, or transport behavior changes, update `README.md`, `docs/ko/README.md`, `skill/agent-bridge/SKILL.md`, `docs/ko/SKILL.md`, `skill/agent-bridge/references/usage.md`, and `docs/ko/usage.md` in the same pull request.

## Commits and pull requests

Use concise, descriptive commit subjects (`feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`). Keep each pull request focused on a single concern, include unit tests under `tests/` for behavioral changes, and reference the tracking issue (`Closes #N`).

## License

Contributions are accepted under the MIT License in [LICENSE](LICENSE).
