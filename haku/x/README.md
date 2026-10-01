# Parked Haku components

This tree holds Haku components removed from active use but kept for historical reference. Each
component README records its status and any reactivation boundary. Nothing in this tree is an active
Flux source by virtue of its location here.

- [`dispatch/`](dispatch/) — retired Haku z.ai dispatch plane and worker-zone perimeter. Its live
  namespaces and resources were pruned after the Flux registrations were suspended and removed.
- [`sandbox_mcp/`](sandbox_mcp/README.md) — retired Console Sandbox MCP, Haku-specific warm pool,
  image, and deployment snapshot. The active resources were removed through the Haku workspaces
  chart; its snapshot under `deploy/` is not reconciled.

The unrelated shared LiteLLM z.ai provider and generic Agent Sandbox constructs remain active.
