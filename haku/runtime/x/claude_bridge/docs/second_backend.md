# A second backend, sketched against Codex

The launch seam (<../backend.py>) exists because a second agent CLI is a near-term target: Codex
ships a comparable application server, and the bridge below the envelope was never
Claude-specific. This note is the honest check on that claim — what a `CodexBackend` would have
to supply, what it would get for free, and where the seam is **not** enough.

Nothing here is implemented. There is no Codex credential in this cluster yet, so none of it has
been run; treat the Codex specifics as read-from-documentation rather than measured, which is the
opposite of how <../../../cli_protocol/protocol.md> was established.

## What the seam asks for

`CliBackend` is three things, and a Codex implementation supplies all three without changing the
protocol:

| member                | Claude                                                       | Codex would be                                       |
| --------------------- | ------------------------------------------------------------ | ---------------------------------------------------- |
| `name`                | `claude`                                                     | `codex` — the `--backend` / `HAKU_CLI_BACKEND` value |
| `resolve(launch)`     | `HAKU_CLAUDE_PATH` + the console's argv, cwd and environment | `HAKU_CODEX_PATH` + the same, plus `CODEX_HOME`      |
| `replayable(payload)` | everything but `stream_event`                                | everything but its own delta kinds                   |

Plus a console-side `build_codex_launch`, the counterpart of `build_claude_launch`: the flags
that mean "speak newline-delimited JSON on stdio, take prompts there, trust this console". For
Codex that is its app-server / protocol mode rather than Claude's
`--output-format stream-json --input-format stream-json`, and its MCP servers arrive through a
config file rather than `--mcp-config`, so `HttpMcpServer.as_config()` — which emits Claude's
`{"type": "http", …}` shape — is Claude's renderer and a Codex session would render the same
console-supplied URL and headers its own way.

## What it gets without doing anything

The envelope and its negotiation, `transport.py`, the runner's dial/serve/redial loop, the
replay window, the workspace bootstrap, stderr forwarding, and the bridge credential being
stripped from the child. `ClaudeLaunch` is already argv + cwd + environment and nothing more, so
the `start` frame carries a Codex launch unchanged; only the class name is Claude-flavoured, and
renaming it is a cosmetic diff across `protocol.py`, `transport.py` and `cli_client.py` that is
best done when a second backend actually lands rather than now.

Deployment is also already per-backend: the runner image and the SandboxTemplate are built
around one CLI, so a Codex sandbox is a second `oci_image` and a second `SandboxTemplate`
setting `HAKU_CLI_BACKEND=codex` and `HAKU_CODEX_PATH`. Nothing about the console's Service, the
claim flow or the bridge route changes.

## Did the sketch force a change to the seam? Yes, once

The first cut of this seam covered launching only — binary, argv, cwd, environment — and left
`DELTA_TYPE = "stream_event"` in the runner, where it decided whether a frame is retained for a
console that adopts the session mid-turn. That is Claude's frame vocabulary sitting on the
generic path, and a Codex backend hits it on day one: its deltas are a different kind, so the
runner would retain them, and an adopting console would be handed text it appends a second time.
Corrupt output, silently, on the recovery path that is hardest to test.

So `replayable` is on `CliBackend` rather than in the runner. The seam is "which CLI, and
therefore which process and which frames", not "which process" alone.

Nothing else in the sketch demanded a change. In particular the `ProcessLaunch` shape survived:
`CODEX_HOME` is an environment entry like `CLAUDE_CONFIG_DIR`, and a config file Codex needs is
something `resolve` can write and point at without the shape growing a field.

## What the seam deliberately does not cover

- **The console-side frame→event adapter.** <../../../../console/x/claude_chat.py> reads Claude's
  `assistant` / `user` / `result` / `system` frames and its `stream_event` deltas directly. A
  second backend needs that behind an adapter too, and it waits on the turn-loop refactor rather
  than being forced through now.
- **The control channel.** `cli_client.ClaudeCli` owns `initialize` and `interrupt` in Claude's
  `control_request` / `control_response` spelling. Codex correlates its own requests differently;
  that is a second seam, in the same place as the adapter above.
- **Choosing a backend per session.** The console imports `build_claude_launch` statically. Which
  CLI a session gets is a product decision that does not exist yet, and inventing a registry on
  the console side before it does would be a mechanism with one caller.

## The environment-variable question

`HAKU_CLAUDE_PATH` and `HAKU_CLAUDE_SETUP` were left alone, and the seam is what makes that
correct rather than merely convenient: the executable variable is a **property of the backend**,
so Claude keeps the name its image already sets and Codex gets `HAKU_CODEX_PATH`. There is no
shared `HAKU_CLI_PATH` to rename anything to.

Worth recording because the brief for this change assumed otherwise: `HAKU_CLAUDE_PATH` is set in
exactly one place, the `runner_image` `env` in <../BUILD.bazel>, and read in exactly one, the
backend. The SandboxTemplate at
<../../../../../cluster/k8s/haku/workspaces/app/sandboxtemplate-haku-claude.yaml> does **not**
set it, so a rename would never have been the cross-image flag day it looked like — but it would
still have broken `haku/console/x/test_claude_bridge_e2e.py`, which sets it, and bought nothing.

`HAKU_CLAUDE_SETUP` is the one genuine misnomer left: the bootstrap it points at checks
haku-state out and knows nothing about which CLI follows it, so it is backend-neutral and its
name says otherwise. Renaming it is a one-line change in the image env and one line in that e2e
test, and it is not worth doing on its own — fold it into whichever change next touches the
runner image.
