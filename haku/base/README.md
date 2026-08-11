# haku/base — shared managed-agent config

**Haku's manual is not here any more.** Who Haku is — goals, persona, voice, hard boundaries — and
how it works — the run procedure, per-source access mechanics, credential discovery — live in the
`haku-state` repo, in the root cards (`AGENTS.md`, `SOUL.md`, `MEMORY.md`) and the hubs they point
at. ducktape holds Haku's **runtime entrypoints** (<../runtime/>) and its **deploy config**, not its
definition. Haku no longer syncs a base from here, and there is no pin to advance.

What is left in this directory is one thing:

- `agent_shared.yaml` — the single source of truth for the shared managed-agent config: model,
  toolset permission policies, and MCP server URLs. The two surfaces that must agree are the
  Anthropic-hosted agent (<../../tf/gitops/haku-cloud-agent/main.tf>) and the self-hosted worker
  (<../runtime/managed_agent/self_hosted/haku.agent.yaml>). `test_agent_config_ssot.py` parses both
  and fails on any drift from this file or between them.

It stays in ducktape deliberately: it sets Haku's model and tool grants, so it must not live in a
repo Haku can write. It is config, not instructions — which is also why the directory name is now a
slight misnomer. Renaming it means touching the Bazel target, both surfaces' comments and the TF,
so it is left for a change that can run the build.
