# Codex app-server launch adapter

Codex runs at the neutral-operation generation (#4667): the runner (`haku/runtime/x/bridge`,
`codex_harness.py` / `codex_projection.py`) starts `codex app-server`, drives its handshake and
turns, and projects its notifications to neutral operations. The Console composes no native protocol
and projects no native frames for Codex.

This package therefore holds only what the Console still owns for Codex:

- `config.py` — the deploy config (`CodexAppServerImplementationConfig`) and the pinned Codex wire
  vocabularies (`ReasoningEffort`, `ApprovalPolicy`, `SandboxMode`).
- `runtime.py` — the launch adapter (`CodexRuntimeAdapter`): `kind`, `display_name`, and
  `build_launch`, which turns the shared runtime's neutral launch facts into Codex's process argv
  plus the `thread/start` params (model, reasoning effort, developer instructions) the runner reads
  from the launch environment.

The native client, protocol/frame vocabulary, projection and capture tooling that used to live here
moved runner-side with the #4667 cut; git holds the pre-cut versions and the protocol-evidence
capture.
