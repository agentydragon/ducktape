# Managed Agents runtimes (loop at Anthropic)

Haku on Anthropic **Managed Agents**: the agent loop runs server-side at
Anthropic; what differs is **where the sandbox (tool execution) runs**. Two
variants, by sandbox location (Anthropic's own vocabulary):

| Dir                                               | Sandbox         | Status                  |
| ------------------------------------------------- | --------------- | ----------------------- |
| [`self_hosted/`](self_hosted/README.md)           | your cluster    | built (Runtime B)       |
| [`anthropic_hosted/`](anthropic_hosted/README.md) | Anthropic cloud | **PARKED** (2026-07-04) |

- **`self_hosted/`** — `worker.py` (anthropic Python SDK) polls the work queue from
  `haku-sandbox`; tools execute there, behind our RBAC + egress perimeter. Brought up
  2026-06. It replaced `ant beta:worker poll`, whose Go SDK deadlocked a session on any
  empty tool result
  ([anthropic-sdk-go#377](https://github.com/anthropics/anthropic-sdk-go/issues/377));
  the Python session runner guards that case, so the deadlock no longer gates this runtime.
- **`anthropic_hosted/`** — Anthropic runs the sandbox too; Haku reaches the
  cluster through a tunneled, `haku`-scoped Kubernetes MCP server (ephemeral pods
  in `haku-sandbox` for in-cluster compute). Sidesteps the self-hosted worker
  entirely; **parked (2026-07-04)** — the cloud control-plane objects were
  deleted at Anthropic and `cluster/k8s/haku/cloud-agent-tf` is suspended; see
  <anthropic_hosted/README.md> for the reason and the resume decision.

The "which runtime" comparison (A / B / C) is <../../plans/runtime_options.md>.
