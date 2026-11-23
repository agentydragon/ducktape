# Crossplane and OpenTofu ownership inventory

Snapshot: 2026-11-23. Crossplane is installed and owns most GitOps OpenTofu
roots through its OpenTofu provider. This is a controller migration: those roots
still use OpenTofu state, provider credentials and provider APIs. Native
Crossplane managed resources replace OpenTofu for LiteLLM keys and teams, and
ExternalDNS owns the former Route 53 record sets.

## Live controller inventory

| Owner                          | Live inventory                                                                  | Status at check                                                          |
| ------------------------------ | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| Crossplane OpenTofu provider   | 18 cluster-scoped Workspaces, 241 HCL resource blocks in inline desired modules | All Workspaces Ready                                                     |
| Crossplane LiteLLM provider    | Nine namespaced Keys and three Teams                                            | All Ready and Synced                                                     |
| Crossplane BuildBuddy provider | One namespaced Secret for live tests                                            | Ready and Synced                                                         |
| tofu-controller                | `gitlab-spike`, `home-switch`, `infra-drift`                                    | Ready, initializing, Ready respectively                                  |
| ExternalDNS                    | Public Route 53 records                                                         | Ownership and adoption documented in [DNS automation](dns_automation.md) |

All three installed Crossplane providers reported Installed and Healthy. Ready
conditions do not prove downstream consumer behavior. The Workspace count is
of desired HCL declarations, not state objects or `for_each` expansions; the
private `thrive-scrape` Workspace accounts for six blocks. The
[tofu-controller roots](../../tf/gitops/) `gitlab-spike` and `home-switch`
contain 27 and 17 blocks in checked-in source, respectively; those counts were
not checked against deployed state. `infra-drift` is outside that count.

The live Workspace names are `agent-machine-access`, `alloy-otlp-bearer-token`,
`budget-ledger`, `cdviz-github-webhook`, `cpap-data`, `finance-agent`,
`flux-webhook-token`, `forgejo-agentydragon`, `forgejo-agentydragon-repos`,
`forgejo-claude`, `forgejo-images`, `gatus-sso`, `github-branch-protection`,
`github-secrets-sync`, `haku-state`, `route53-domain-delegation`,
`sso-providers`, and `thrive-scrape`.

## How ownership changed

- cdk8s renders inline Workspace modules from GitOps roots. Flux applies them
  and Crossplane reconciles their OpenTofu state. See
  [Workspace source flow](crossplane-opentofu-artifact-triggers.md).
- LiteLLM Keys and Teams are native Crossplane resources; the old `litellm-keys`
  root is gone. BuildBuddy's live-test secret is also a native managed resource.
  See [LiteLLM key definitions](../cdk8s/litellm/crossplane_keys.py).
- ExternalDNS manages the former Route 53 records. Domain registration remains
  in the `route53-domain-delegation` Workspace.
- The previous Kubernetes-secret roots for Ollama, the Gaffer GHCR pull Secret,
  and LiteLLM master/salt Secrets were retired in #6049, #6053 and #6061.
  Those historical migrations do not prove that any obsolete state or credentials
  have since been removed; verify the specific resources before cleanup.

## Remaining work

The 241 Workspace blocks include 114 Authentik blocks (three are tokens), 38
Kubernetes Secret blocks, 24 random-password blocks and one random-ID block.
These categories are inventory counts, not a safe conversion total. Authentik
Blueprints need a proven OAuth credential handoff; Secret migrations need an
explicit owner transfer and rotation plan. Forgejo and GitHub objects still
have OpenTofu provider owners. Do not infer that a Workspace's Ready condition
permits deleting its state, controller permissions or credentials.

For migration sizing, a live per-root inventory and current candidate review,
see [the conversion inventory in PR #7421](https://github.com/agentydragon/ducktape/pull/7421).
Recheck current state and consumer health at the time of any handoff. In
particular, `home-switch` was initializing at this snapshot and private
`thrive-scrape` contents were counted but not assessed.
