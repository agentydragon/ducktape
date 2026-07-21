# Kagent retirement

Kagent was parked on 2026-05-08 and retired from the active cluster configuration on
2026-07-21. This directory is a historical snapshot and is not reconciled by Flux.

## Why it was retired

Kagent did not enforce a client-side budget on tool results. A noisy MCP call such as
`kubectl get events`, a full pod listing, or a large log response was stored verbatim in
the session history. The next model request then exceeded the z.ai coding-plan prompt
limit and failed with error `1261 - Prompt exceeds max length`, killing the session.

The available compaction was interval-based and ran between user turns. It could not
protect a single turn whose tool calls had already exceeded the prompt budget. The stock
Kubernetes agents also began with large system prompts and tool schemas, leaving less
room for results. Together, those properties made Kagent too fragile for the noisy
cluster-operations workload it was intended to handle.

## Retirement state

- Workloads, database, CRDs, namespace resources, and experimental `devbot` manifests
  had already been removed or suspended before archival.
- The obsolete Authentik proxy provider and standalone outpost were reconciled absent;
  their outpost Deployment, Pod, and Service were verified absent.
- Archival removes the suspended Flux Kustomizations from the root cluster wiring and
  removes the unused Terraform-owned Kagent OIDC application, provider, and reflected
  oauth2-proxy Secret.

Do not restore these files directly into the active tree. Re-evaluate the current
upstream release first. A revival should require token-budget-aware tool-output bounding
that operates before results enter session history, plus an end-to-end noisy-MCP test.

## Contents

- `k8s/kagent/` — suspended Helm, Flux, database, namespace, and secret manifests
- `k8s/devbot/` — unreconciled Kagent `Agent`/`ModelConfig` desktop experiment
- `terraform/provider_kagent.tf` — retired Authentik OIDC and oauth2-proxy Secret wiring
- `docs/operational_findings.md` — observed failure mode and possible revival criteria
- `docs/kagent_sso.md` — final SSO topology and legacy-outpost retirement record
- `docs/kagent_persistent_agents.md` — exploratory persistent-agent design
