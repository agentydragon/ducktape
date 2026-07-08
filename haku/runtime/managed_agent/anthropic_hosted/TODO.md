# Managed Agents — Anthropic-hosted cloud TODO

Status: **PARKED (2026-07-04)** — the cloud control-plane objects were deleted at
Anthropic by the operator, and `cluster/k8s/haku/cloud-agent-tf` is suspended. See
<PLAN.md> for the reason and the resume decision. The items below describe the
pre-parking v0 state and are **moot until the plan is resumed**:

- **Tighten cloud egress.** The environment had `networking.type: unrestricted`
  (TODO in main.tf). Narrow to `type: limited` + an explicit `allowed_hosts` if/when
  recreated. (The static_bearer MCP credential is already scoped — only presented to
  the `kubectl-machine-mcp` URL — so this was hardening, not a leak fix.)
- **Build the run loop (P1–P3).** Ephemeral-pod compute, the cloud run procedure,
  and a scheduled wake — see <PLAN.md>. Before parking, the agent only ran the v0
  connectivity test.

The Sonnet pin (`model: claude-sonnet-4-6`) was intentional for bring-up test runs
— not a TODO in its own right; revisit the model if/when the runtime resumes past v0.
