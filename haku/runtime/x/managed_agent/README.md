# Managed Agents runtimes (parked experiments)

Haku on Anthropic **Managed Agents**: the agent loop runs at Anthropic; the variants differ in
where the sandbox (tool execution) runs. Both are **parked** (2026-07-04) pending a reviewed
reactivation decision; comparison with other runtimes: <../../../plans/runtime_options.md>.

- [`self_hosted/`](self_hosted/README.md) — `worker.py` (anthropic Python SDK) polls the work
  queue in `haku-sandbox`, behind our RBAC + egress perimeter. Unwired: no Flux registration.
- [`anthropic_hosted/`](anthropic_hosted/README.md) — sandbox in Anthropic's cloud; reaches the
  cluster via a tunneled `haku`-scoped Kubernetes MCP server (ephemeral pods in `haku-sandbox`).
  Docs only: objects deleted at Anthropic 2026-09-30; Terraform root and its parked CR deleted
  2026-11-21 (provider dormant). Resuming: see its README.
