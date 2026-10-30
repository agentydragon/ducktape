# Executor API

[`executor.proto`](executor.proto) defines the sandbox-owned execution service. The Runner
implements it; Runner Gateway proxies the same contract with its existing caller authorization,
Sandbox routing and incarnation fencing. Gateway terminates and reissues gRPC transport calls;
the Runner owns execution and translates to the sandbox's configured local Codex exec-server.

`OpenSession` holds one adapter connection and its local WebSocket. It initializes a native Codex
session or resumes the requested session. Process and file operations are typed unary RPCs.
All adapter connections in one sandbox share that sandbox's exec-server process. An execution
session is separate from a persisted sampler conversation and does not create another server or VM.

Closing the stream releases the adapter's connection and workers. The sampler does so when it has
no live cells or processes, and cancellation, observer fencing and Runner drain also close it.
Codex 0.162.1 retains a detached native session for 30 seconds to allow reconnection; on expiry it
removes the session and terminates its remaining processes. File handles are connection-owned.
See upstream [session lifecycle](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server/src/server/session_registry.rs)
and [connection cleanup](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server/src/server/handler.rs).

The adapter retains the previous relay's admission budget of eight concurrent open or opening
connections per Runner. This is an inherited Agentplane resource guard, not a Codex session limit
or a lifetime quota on conversations. Connections consume streams, sockets, goroutines and bounded
queues; native processes consume the sandbox's existing resource budget. Active connections block
autosuspend. Native frames remain limited to 1 MiB with bounded notification backpressure.

Request IDs correlate calls and evidence without deduplication or admission receipts. The adapter
adds no persistence. Lost mutation replies remain outcome-unknown and are not automatically replayed.
