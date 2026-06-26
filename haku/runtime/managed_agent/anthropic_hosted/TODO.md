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

## Resolved

- **Token propagation (was the blocking concern).** The rotated `haku-k8s` JWT now
  reaches the Anthropic vault automatically: `authentik-jwt-rotation` writes the
  `haku-cloud-kube-token` Secret → Flux applies it → the tofu root reads it
  in-cluster and re-sends it into the vault as a TF 1.11 write-only attribute. No
  CronJob-calls-`ant` step.
- **IaC adoption.** We adopted `modus-agendi/anthropic-claude-managed-agents`
  (pinned + hash-locked) to manage the agent/vault/credential declaratively —
  reversing the earlier "stay with scripting, re-evaluate once it has adoption"
  call. It's a low-user-count community provider, so **review the source diff on
  every version bump** before repinning (see memory
  `project_acma_provider_repin_review` and the caution in `terraform.tf`).
- **Off Path B onto the MCP path.** v0 first `curl`ed kube-apiserver directly
  (`aud=kubectl-sandbox-client-credentials`); now it goes through
  `kubectl-machine-mcp`, which trusts that issuer. No second audience needed.
