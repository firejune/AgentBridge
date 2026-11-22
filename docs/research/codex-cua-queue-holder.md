# A CUA app-server can hold the Desktop queue

Research for [Agent Bridge #210](https://github.com/firejune/AgentBridge/issues/210), 2026-11-22.

- [source] The public tag matching the bundled CLI version is `rust-v0.171.0-alpha.13` at `581161ab41e643d942e1e1b5f561034060bf18e3`. The server's `stdio://` listener uses the normal request processor, including `thread/resume`. With a local SQLite store, the queue watcher checks threads loaded in that server. The tagged cold-resume test asserts that a queued item stays pending through metadata reads, then runs and leaves the queue when the thread is resumed. [Release][release] · [Stdio listener][stdio] · [Resume handler][resume] · [Queue wiring][wiring] · [Watcher][watch] · [Cold-resume test][test]
- [observed] The installed macOS bundle reported `26.1117.41037`, build `29803`. The bundled CLI and `node_repl` passed signature verification. Static strings matched the CLI version and the REPL's stdio launcher. One process held the queue in the read-only snapshot. The issue records a separate observation of the bundled REPL's stdio app-server sharing the queue store and operator recovery after stopping that process. [Two-holder observation][trigger] · [Operator recovery][recovery]
- [inference] The bundled stdio child has the app-server capability to load an eligible saved thread and dispatch its queued input. The Desktop writer lock protects a thread Desktop already holds. The client's ordinary resume behavior and the installed binary's exact compiled commit remain unverified. Keep delivery refused while queue ownership is ambiguous. [Writer-lock study](codex-writer-lock.md) · [Queue dispatch study](codex-unloaded-thread-queue.md)

[release]: https://github.com/openai/codex/releases/tag/rust-v0.171.0-alpha.13
[stdio]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/app-server/src/lib.rs#L835-L850
[resume]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/app-server/src/message_processor.rs#L1445-L1454
[wiring]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/app-server/src/message_processor.rs#L349-L408
[watch]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/ext/queue/src/service.rs#L90-L191
[test]: https://github.com/openai/codex/blob/581161ab41e643d942e1e1b5f561034060bf18e3/codex-rs/app-server/tests/suite/v2/thread_queue.rs#L600-L642
[trigger]: https://github.com/firejune/AgentBridge/issues/210#issuecomment-7076471325
[recovery]: https://github.com/firejune/AgentBridge/issues/210#issuecomment-7076183042
