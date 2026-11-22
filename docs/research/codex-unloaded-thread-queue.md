# Codex Desktop dispatches queued input after the thread loads

Research for [Agent Bridge #221][issue], 2026-11-22.

[observed] After a Desktop restart, #221's self-archive entry waited in the queue from 11:20:24 UTC until the owner opened the saved thread in the sidebar. The archive was recorded at 11:24:53 UTC and `close --finish` verified it. This is the measured case for self-archive. [Issue observation][issue]

[source] The installed macOS app's Info.plist identifies version `26.1117.41037`, build `29803`. The [writer-lock study][writer-lock] identifies its bundled CLI as `0.171.0-alpha.13`. The Rust references below pin the corresponding public tag's commit, `581161ab41e643d942e1e1b5f561034060bf18e3`. The installed binary's exact compiled commit is unverified. Bundle offsets below are byte offsets within the packed ASAR members. [OpenAI release][release]

## 1. Queue consumer and dispatch condition

- [source] The Codex app-server wires a SQLite-backed `LocalQueueStore` into `QueuedItemService` in `app-server/src/message_processor.rs`. The local store uses `queue_1.sqlite`. `ext/queue/src/lib.rs::install` registers the lifecycle contributor and starts one external-message watcher in each such server. The watcher detects database changes process-wide, and dispatch is scoped to threads already loaded by that server. [App-server wiring][wiring] · [Queue database][db] · [Extension installation][install]
- [source] `QueuedItemService::watch_external_messages` polls SQLite's `PRAGMA data_version` on a ten-second interval and queries revisions using `ThreadManager::list_thread_ids`. Both that list and `get_thread` use the manager's in-memory runtime map. The watcher also checks newly loaded or resumed IDs from revision zero, so a row written while a thread was unloaded is eligible when that thread later loads, even if no new database change occurs. The watcher schedules work per thread and stops that attempt if the thread is missing, running, interrupted, shut down, or marked not found. [Watcher discovery][watch] · [SQLite revision filter][revisions] · [Runtime map][runtime-map] · [Watcher dispatch][watch-dispatch]
- [source] `wake_if_loaded` asks the runtime to emit an idle lifecycle event. `on_thread_idle` skips interruptions and takes a per-thread dispatch guard. `dispatch_if_idle` fetches the head record for that thread and calls `start_turn_if_idle` with trigger `queue`. For a valid item, it deletes the row after the turn starts. The tagged test `cold_thread_resume_dispatches_a_persisted_queued_submission` explicitly checks that the row survives metadata reads with status `notLoaded`, then checks that `thread/resume` starts that input and the queue empties. [Idle eligibility and dispatch][dispatch] · [Lifecycle hooks][hooks] · [Upstream regression test][cold-test]
- [inference] In the #221 observation, the Desktop-owned app-server consumed the row after the sidebar activated the saved thread. The upstream logic explains the wait and release. The ten-second source interval supplies neither a measured latency for build 29803 nor a delivery deadline. [Issue observation][issue] · [Watcher discovery][watch]

## 2. Ordinary Agent Bridge delivery

[source] After the eligible default-IPC connection failure, `codex_live.py::deliver` calls `codex_queue.py::deliver`. Ordinary delivery passes a fixed retrieval notice to the same `enqueue` helper as `enqueue_self_archive`. Both invoke the bundled CLI with `queue --disable daemon_auto_start --thread … --message …`, without `--resume` or a Desktop URL. The CLI submits text as `ThreadQueueAdd`. The shared consumer checks the target's lifecycle and the input type. A valid notice and a valid self-archive prompt have the same load and idle eligibility. [IPC fallback][bridge-fallback] · [Bridge enqueue paths][bridge-queue] · [CLI text submission][cli-queue] · [Queue dispatch][dispatch]

[inference] On build 29803, an ordinary notice queued to a saved thread that remains unloaded after restart will wait for it to load. #221 measured the self-archive prompt. A successful `queued_id` establishes acceptance. Broker readback requires the addressed turn to execute `receive-codex`. No fresh ordinary-delivery restart measurement was made for this study. [Issue observation][issue] · [Bridge queue protocol][bridge-read]

## 3. Ways to load a thread from outside Desktop

| Candidate | Evidence and scope | State writes |
| --- | --- | --- |
| Legacy Desktop follower IPC | [source] The bridge calls `thread-owner-discovery` for the exact conversation and requires an owner advertising `supportsUntrustedAppInput` before sending `thread-follower-steer-turn` or `thread-follower-start-turn`. These calls submit to an owner the bridge already found. The adapter has no load RPC. [observed] #156 records an unavailable legacy endpoint on the preceding build. Build 29803's adoption evidence used the queue and URL, and this study identified no supported external Desktop IPC load method. [Bridge owner check][bridge-owner] · [Bridge submission][bridge-fallback] · [IPC observation][ipc-issue] · [Build 29803 validation][wake-pr] | [source] The bridge's follower calls are protocol requests and require no direct database edits. [Bridge submission][bridge-fallback] |
| Documented navigation URL | [source] OpenAI documents `codex://threads/<thread-id>` to open a local chat. In build 29803, `G` parses the `localConversation` route in `.vite/build/early-bootstrap-916-90e0adb52d70fbc5.js` (SHA-256 prefix `69e6d73862d50883`, byte 3083095 onward). The route navigates the Desktop window in `.vite/build/worker.js` (`8324aeca1d6d5d40`, byte 76021 onward). [inference] This is a supported way to open the thread without a sidebar click. Queue dispatch on this build still depends on the thread loading and being eligible. [OpenAI supported links][links] · [Queue dispatch][dispatch] | [source] The caller hands the OS a URL. Desktop owns the navigation and any resulting thread activity. [OpenAI supported links][links] |
| Build-specific queue wake URL | [source] In the same bootstrap member, `G` parses `codex://threads/wake/<thread-id>` as `wakeQueuedThread`. The manager `X` schedules it outside normal window navigation (bytes 3087134–3088020). In `worker.js`, the callback connects to Desktop's app-server, checks queue support, calls `thread/queue/list` for the exact ID with limit 1 and calls `resumeThread` only if the queue is nonempty (byte 71442 onward). The internal `resumeThread` implementation emits `thread/resume` with `excludeTurns: true` in `.vite/build/chat-search-worker.js` (`4dc2775808cc53bd`, byte 729897 onward). [observed] The existing bridge adoption path uses that URL, and #191 records a completed exact wake turn on a newly created thread on build 29803. The URL is absent from OpenAI's published supported-links list. [Bridge adoption][bridge-wake] · [Build 29803 validation][wake-pr] · [OpenAI supported links][links] | [source] Opening the wake URL requires no direct state edit. Desktop performs the resume and queue dispatch. [Bridge adoption][bridge-wake] |
| App-server RPC and `codex queue --resume` | [source] `thread/resume` is a documented RPC that loads a saved thread into the server that receives the request. `thread/read` can return saved metadata without loading it. The documented CLI `--resume` option requires a local daemon or an explicit `--remote` endpoint, and a failed resume can still yield successful enqueue status. The 29803 adoption record shows the Desktop-owned server started without `--listen`; the tagged transport defaults to stdio. No supported external address for that Desktop-owned process was established. [App-server guide][server-guide] · [CLI resume implementation][cli-resume] · [Build 29803 validation][wake-pr] · [Tagged transport default][stdio] | [source] The RPC needs a connection to the intended server and no direct state edits. [inference] Starting or using a different server can load the thread in the wrong process or contend for its writer lock. [App-server guide][server-guide] · [Writer-lock study][writer-lock] |

## 4. Recommendation

[inference] Keep this follow-up documentation-only. The [merged note][existing-note] already tells operators to open an unloaded thread for pending self-archive. Apply the same guidance to ordinary notices that were accepted and remain unread. The documented navigation link can open the exact chat when the operator wants it visible. Keep the existing build-pinned wake path limited to verified startup adoption, and preserve the original queued entry while the thread loads.

## Remaining live verification

[inference] A separately authorized test could use a disposable Desktop profile and two saved, idle threads with empty queues. After the profile's Desktop restarts, submit one ordinary bridge notice to each exact thread and record acceptance plus the absence of a new turn while they remain unloaded. Open the documented navigation link for one and the build-specific wake link for the other, once each. Check which exact thread runs each notice and whether the broker records `received`. This would distinguish ordinary cold delivery from the prior self-archive and new-thread results. Opening the navigation link can change the visible chat. Either link can start a model turn and run queued instructions, so only isolated test threads with known queue contents are suitable. No live probe was performed for this study.

[issue]: https://github.com/firejune/AgentBridge/issues/221
[writer-lock]: codex-writer-lock.md
[release]: https://github.com/openai/codex/releases/tag/rust-v0.171.0-alpha.13
[wiring]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/app-server/src/message_processor.rs#L349-L408
[db]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/state/src/sqlite.rs#L48-L54
[install]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/ext/queue/src/lib.rs#L13-L21
[watch]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/ext/queue/src/service.rs#L90-L191
[revisions]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/state/src/runtime/queued_items.rs#L35-L76
[runtime-map]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/core/src/thread_manager.rs#L2044-L2083
[watch-dispatch]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/ext/queue/src/service.rs#L194-L245
[dispatch]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/ext/queue/src/service.rs#L440-L522
[hooks]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/ext/queue/src/service.rs#L573-L605
[cold-test]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/app-server/tests/suite/v2/thread_queue.rs#L516-L642
[bridge-fallback]: ../../skill/agent-bridge/scripts/codex_live.py#L346-L399
[bridge-queue]: ../../skill/agent-bridge/scripts/codex_queue.py#L112-L169
[cli-queue]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/tui/src/session_queue_commands.rs#L97-L142
[bridge-read]: ../../skill/agent-bridge/SKILL.md#2-codex--chatgpt-desktop-codex
[bridge-owner]: ../../skill/agent-bridge/scripts/codex_live.py#L267-L287
[ipc-issue]: https://github.com/firejune/AgentBridge/issues/156
[links]: https://learn.chatgpt.com/docs/app/commands#deep-links
[bridge-wake]: ../../skill/agent-bridge/scripts/codex_wake_open.py#L301-L325
[wake-pr]: https://github.com/firejune/AgentBridge/pull/191
[server-guide]: https://learn.chatgpt.com/docs/app-server#resume-a-thread
[cli-resume]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/tui/src/session_queue_commands.rs#L84-L94
[stdio]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/app-server-transport/src/transport/mod.rs#L122-L153
[existing-note]: https://github.com/firejune/AgentBridge/pull/222
