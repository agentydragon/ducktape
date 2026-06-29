# Managed Agents — Anthropic-hosted cloud TODO

P0 shipped: the cloud control plane is Terraform-managed
(<../../../../tf/gitops/haku-cloud-agent>) and the agent reaches the cluster
through `kubectl-machine-mcp` as `haku`. The phased build plan is <PLAN.md>.
Open items:

- **Tighten cloud egress.** The environment has `networking.type: unrestricted`
  (TODO in main.tf). Narrow to `type: limited` + an explicit `allowed_hosts`. (The
  static_bearer MCP credential is already scoped — only presented to the
  `kubectl-machine-mcp` URL — so this is hardening, not a leak fix.)
- **Build the run loop (P1–P3).** Ephemeral-pod compute, the cloud run procedure,
  and a scheduled wake — see <PLAN.md>. The agent today only runs the v0
  connectivity test.

The Sonnet pin (`model: claude-sonnet-4-6`) is intentional for bring-up test runs
— not a TODO; revisit the model once the runtime is past v0.
