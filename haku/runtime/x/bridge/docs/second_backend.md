# The second harness

Codex app-server is the second harness on the shared runner's per-harness `run()` seam
(<design.md> § The runner seam). Each harness owns its whole run-loop behind
`Harness.run(launch, session)` (<../backend.py>): it starts its binary, speaks its native protocol
end to end — handshake and all — and emits neutral operations through the <../session_api.py>
`SessionApi`. The runner interprets no native payload and drives no turn.

`ClaudeHarness` (<../claude_harness.py>) and `CodexHarness` (<../codex_harness.py>) are parallel at
the edges and share the middle:

- both start a subprocess and speak newline-delimited JSON over stdio, so the stdio pump
  (`start_process`, `read_json_frames`, `forward_stderr`, `shutdown`, `StdinWriter`) is shared in
  <../backend.py>;
- each owns its native protocol — Claude's one-frame `initialize`, Codex's stateful
  `initialize`/`initialized`/`thread/start` handshake and turn loop — and what a dispatched prompt
  and an interrupt are written as;
- each projects its own native frames to the neutral operations of <../neutral_operations.py>
  (<../claude_projection.py>, <../codex_projection.py>) and journals them through `SessionApi`,
  which owns the one dense sequence, retention, the journal, the abort rewrite and the admission
  fence — no harness reimplements those;
- harness identity is fixed out of band by `--harness` when the runner starts, never repeated around
  a native message.

## Codex's shape, derived from the app-server protocol

- **One thread per session.** `thread/start` runs once at handshake and captures the
  server-assigned `threadId`; every turn binds to that one thread.
- **Turns are the runner's brackets.** Codex opens no turn on its own stream, so a turn opens only
  when a prompt is admitted (`turn/start`, `PromptsCause`) and closes on `turn/completed`. Prompts
  are queued and started one at a time — a `turn/start` only lands with no turn in flight — and the
  operator's interrupt is a `turn/interrupt` on the active server `turnId`, sent out of band so it
  need not wait behind a queued prompt.
- **`turn/completed` carries the outcome**: `completed` → answered, `interrupted` → aborted,
  `failed` → failed with the `TurnError.message`. Its notification stream (`item/started`, the
  `agentMessage`/`reasoning`/`commandExecution` deltas, `item/completed`) folds to item lifecycles;
  Codex-specific item classes are counted in the batch diagnostics, not crashed on.
- **`thread/start` params travel in the launch environment.** The runner owns `thread/start` now, so
  the model, reasoning effort and developer instructions the Console used to send with its own
  `CodexThread` ride the launch environment under `codex_options`' keys (<../codex_options.py>);
  the process argv still carries the provider and MCP config.

## Deployment

Codex is deployed and launched today. The `codex_app_server` runtime block
(`cluster/k8s/haku/console/config.yaml`), its `SandboxTemplate` and `WarmPool`
(`cluster/k8s/agents/agent-sandbox/workspaces/app/sandboxtemplate-codex.yaml` and
`sandboxwarmpool-codex.yaml`), the dedicated least-credential codex-runner proxy, the migrations
permitting `runtime_kind = codex_app_server`, and its access profile all exist in the cluster, and
the Console launches Codex runner pods. What was missing was the runner's Codex driver: a launched
runner refused at start because `codex-app-server` had no way to interpret its own stream. This
change supplies it — the runner-side handshake, turn loop and projection above — so a launched Codex
runner serves its session.
