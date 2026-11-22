# Deploying a supervised install

This is the procedure for moving a machine that runs Agent Bridge under `launchd` to a new
commit. `tools/deploy.py` performs it. Run the script from a clean checkout; do not do the
steps by hand.

A development checkout that links `skill/agent-bridge` directly and runs `serve` in a
terminal needs none of this.

## What is installed, and where each part is pinned

Releases are plain `git archive` extractions named by the full commit id.

| Part | Runs from | Pinned by |
| --- | --- | --- |
| CLI and skill | `~/.local/share/agent-bridge/nana-runtime/releases/<commit>/skill/agent-bridge` | Three symlinks: `~/.agents/skills/agent-bridge`, `~/.codex/skills/agent-bridge`, `~/.claude/skills/agent-bridge` |
| Broker | `~/.local/share/agent-bridge/broker-runtime/releases/<commit>/…/bridge.py serve` | `ProgramArguments` of `~/Library/LaunchAgents/com.agent-bridge.broker.<socket hash>.plist` |
| MCP facade | `…/nana-runtime/releases/<commit>/…/mcp_server.py`, started by the tunnel client | One command line in the tunnel profile (`~/.config/agent-bridge/nana/tunnel.yaml`), run by its own launchd agent |

The tunnel profile also holds a credential. The script rewrites only the release id in it,
never prints the file and never copies it.

The broker state (`~/.local/share/agent-bridge/bridge.state`) and the facade state
(`nana-runtime/state`) are not part of a release and are not moved.

## One commit for every part

The default moves all three parts to the same commit, which must be on `origin/main`.
A CLI that runs against a broker of another build is the failure this procedure exists to
prevent (issue #156); `ping` reports it as `BROKER_BUILD_SKEW`.

`--components cli,broker,facade` selects a subset. A partial move that leaves two builds
running ends with exit 1 and says so in the receipt.

If a live release is not an ancestor of the target, for example a broker running a commit
that was never merged, the script refuses with `LIVE_NOT_IN_TARGET` and names how many
commits would be dropped. Merge them first, or pass `--allow-non-ancestor` to drop them on
purpose. The same flag is needed to move back to an older commit.

## Run it

```bash
git fetch origin
python3 tools/deploy.py --commit <commit> --dry-run   # prints every step, changes nothing
python3 tools/deploy.py --commit <commit>
```

Exit codes: `0` deployed and verified, `1` a step or a check failed after changes began,
`2` refused before anything changed.

## Refusals (nothing is changed)

| Code | Meaning |
| --- | --- |
| `SOURCE_UNKNOWN` | The commit is not in this checkout. |
| `SOURCE_DIRTY` | The checkout the script runs from has uncommitted changes. |
| `SOURCE_NOT_ON_MAIN` | The commit is not contained in `origin/main` (unpushed or unmerged). |
| `TARGET_ALREADY_LIVE` | Every selected part already runs the commit. |
| `LIVE_NOT_IN_TARGET` | A live release carries commits the target lacks. |
| `PIN_NOT_A_LINK`, `PINS_DISAGREE` | A skill pin is not a symlink, or the pins point at different releases. |
| `BROKER_SERVICE_MISSING`, `BROKER_SERVICE_UNREADABLE` | The broker plist is absent or names no single release. |
| `FACADE_PROFILE_MISSING`, `FACADE_PROFILE_UNREADABLE`, `FACADE_SERVICE_MISSING` | The tunnel profile or its launchd agent cannot be identified. |
| `BROKER_UNREACHABLE` | The running broker did not answer `ping` or `retention --live`. |
| `CORRESPONDENCE_LOST` | The broker already reports lost correspondence. |
| `DELIVERIES_IN_FLIGHT` | A message is `dispatching`. Stopping now would leave it uncertain. |
| `CLOSE_HANDOFF_OPEN` | A route is mid close handoff and its operator request may still move (`dispatching`, `submitted`, `in_progress` or `uncertain`). A stranded reservation, with no request or a failed or undelivered one, survives the restart and does not block; release it with `close --finish ... --saved-state`. |
| `WAITS_ACTIVE` | A caller is blocked in `ask` or `wait`; a restart would cut it off. |
| `RELEASE_MISMATCH` | A release directory with this id exists and differs from the commit. |
| `RELEASE_UNUSABLE` | The new CLI could not ping the running broker. |

The three in-flight checks apply only when the broker or the facade moves.

## Steps of a full deploy

1. Read the live pins, broker plist and tunnel profile; check the source and the refusals above.
2. Extract the commit into `nana-runtime/releases/<commit>` and `broker-runtime/releases/<commit>`
   (staged beside the final place, then renamed). If a release directory with that id already
   exists it must match the commit; `__pycache__` folders and `.pyc` files written by an earlier
   run are ignored in that comparison. The new CLI must be able to ping the broker.
3. Snapshot into `~/.local/share/agent-bridge/deploy-<commit7>-<random>/`: `routes.json`,
   `correspondence.json`, `correspondence-index.json`, `policy.json`, the broker plist, the
   facade plist, the facade's `events.json` and `ledger.sqlite3`, `cli-pins.json` (the pin
   targets before the move) and `facade.json` (the release id the tunnel profile named before
   the move). The tunnel profile itself is not copied, because it holds a credential. The
   rollback command is printed here.
4. Stop the facade: `launchctl bootout gui/<uid>/<facade label>`.
5. Stop the broker: `launchctl bootout gui/<uid>/<broker label>`. The broker handles the
   signal by writing its checkpoint; the script waits until launchd no longer lists the service.
6. Rewrite the release id in the broker plist. Every other key is kept.
7. Start the broker: `launchctl bootstrap gui/<uid> <plist>`, then wait for `ping`.
8. Move the three pins (each replaced atomically).
9. Rewrite the release id in the tunnel profile and start the facade.
10. Verify and write the receipt.

Nothing is retried. If a step fails the run stops there, writes the receipt with `error`,
and prints the rollback command.

## What is verified

- Pins, plist and profile all name the target commit.
- `ping` through the new pin answers; `build` equals `cli_build`; there is no warning.
- `correspondence_lost` is false.
- Every endpoint saved in `routes.json` before the restart is still there, none was added, and the channel count is unchanged. One exception is a warning, not a failure: the broker does not restore a route whose workspace folder no longer exists, so such a route missing after the restart is reported as a count (`routes_dropped_workspace_missing`).
- No `pending` or `uncertain` message disappeared from `correspondence.json`.
- The facade's `/readyz` answers (through `nana-runtime/health.url`).

`result.json` in the snapshot folder records these with the commit ids before and after,
the broker and tunnel PIDs, the message counts by state, the subscriber counts, the time
each `launchctl` action took, and the total elapsed time.

Watchers (`watch` streams) are disconnected by a broker restart. The receipt records how
many were attached before and after; a watcher that does not reconnect has to be restarted
by its owner.

## Rollback

```bash
python3 tools/deploy.py --rollback ~/.local/share/agent-bridge/deploy-<commit7>-<random> --dry-run   # prints the steps, changes nothing
python3 tools/deploy.py --rollback ~/.local/share/agent-bridge/deploy-<commit7>-<random>
```

It restores the pins and the broker plist from that snapshot, rewrites the release id in the
tunnel profile back to the one in `facade.json` (in place, like the deploy does; the rest of
the profile is not touched), and restarts the services that changed. It works when the pins
point at different releases, which is what an interrupted deploy leaves. It does not restore the state files: the broker keeps
the correspondence it has. The copies in the snapshot folder are for a person to inspect if
state itself is damaged.

Use it when the bridge does not answer after a deploy. A deploy that ends with exit 1 but
an answering broker is a judgment call; read `error` in the receipt first.

## Not covered

- Installing the services for the first time (`install-service`; see usage.md).
- The tunnel client binary and the facade configuration (`phase1.json`).
- Clearing old release and snapshot folders.
