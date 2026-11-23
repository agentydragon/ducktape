# In-cluster OpenTofu conversion inventory

Snapshot: 2026-11-23. This replaces the September inventory. Counts are HCL
`resource` blocks, not instantiated objects: `for_each`, `count`, modules, state
and provider-side objects can change the actual count. The inventory is a basis
for evaluating migrations, not a claim that every block can safely be transferred.

## Current controllers and inventory

The cluster has 18 cluster-scoped Crossplane OpenTofu Workspaces, all reporting
`Ready=True`, with **241 resource blocks** in their live inline modules. The
private `thrive-scrape` Workspace contributes six; its contents were counted
in the live module but not assessed. These are desired Workspace modules, not
an independent audit of OpenTofu state or downstream service health.

| Workspace                  | Blocks | Workspace                 | Blocks |
| -------------------------- | -----: | ------------------------- | -----: |
| agent-machine-access       |     56 | alloy-otlp-bearer-token   |     11 |
| budget-ledger              |      6 | cdviz-github-webhook      |      3 |
| cpap-data                  |     10 | finance-agent             |     12 |
| flux-webhook-token         |      3 | forgejo-agentydragon      |      1 |
| forgejo-agentydragon-repos |      4 | forgejo-claude            |      4 |
| forgejo-images             |      2 | gatus-sso                 |      4 |
| github-branch-protection   |      1 | github-secrets-sync       |     20 |
| haku-state                 |     19 | route53-domain-delegation |      1 |
| sso-providers              |     78 | thrive-scrape             |      6 |

Separately, tofu-controller has `gitlab-spike` (Ready) and `home-switch`
(Ready unknown, initializing); their checked-in roots contain 27 and 17 blocks
respectively. These are **source counts**, not verified deployed counts. The
`infra-drift` Terraform CR is excluded, as in the original audit. Thus 285 is
the combined visible desired-block count (241 + 44), not a deployed-state count.
The inactive `augur-evidence` source root has nine blocks and is excluded.

Crossplane also manages native LiteLLM and BuildBuddy resources: the live check
found nine Keys, three Teams and one BuildBuddy Secret, all Ready and Synced.
These are managed resources, not OpenTofu blocks. ExternalDNS now owns the
previously identified Route 53 records; domain registration remains the one-block
Workspace. See [DNS automation](../cluster/docs/dns_automation.md) and
[Workspace source flow](../cluster/docs/crossplane-opentofu-artifact-triggers.md).
Moving a root to a Crossplane OpenTofu Workspace changes the controller, but
retains OpenTofu, provider credentials and state; it is not a conversion away
from OpenTofu.

## Recalculated opportunities

The 241 live Workspace blocks include **114 Authentik blocks**: 34 applications,
38 policy bindings, 14 OAuth providers, three proxy providers, 12 groups, seven
users, one brand, one OAuth source, one scope mapping and three tokens. The
**111 non-token blocks** are a theoretical Blueprint review pool (46.1% of 241),
not a ready migration estimate. Each needs an ownership and dependency review;
OAuth client credential delivery remains a separate problem. Three token blocks
need creation-time key capture and durable delivery, which ordinary Blueprint
export does not establish. A Blueprint transfer that leaves Terraform's Secret
writers in place does not remove the whole root.

The Workspace modules also contain **38 Kubernetes Secret blocks**, 24 random
password blocks and one random ID block. These counts overlap neither the 114
Authentik blocks nor each other, but are **not** a sum of safe conversion
candidates: some carry provider-generated tokens, OAuth secrets, webhook tokens,
or values used by external APIs. `for_each` further means a block can produce
multiple Secrets. Review each credential's producer, consumers and rotation
path before assigning it to ESO.

Concrete password/Secret pairs to assess include `budget-ledger` (2 blocks),
`cpap-data` (4), `finance-agent` Forgejo users (4), and `forgejo-claude` (3,
one password and two Secrets): **13 blocks / 241 = 5.4%** as a scoped review
pool, not a verified migration plan. Forgejo user password updates and consumer
Secret changes must be coordinated. `haku-state`'s current password and Secrets
serve Forgejo, not the former Haku Console API password; the prior 2-block
candidate does not carry over without a new dependency review. Webhook-token
pairs in `flux-webhook-token`, `cdviz-github-webhook`, `finance-agent`, and
`haku-state` additionally require a coordinated webhook handoff.

The remaining Forgejo and GitHub resources still use OpenTofu provider APIs.
Native Crossplane LiteLLM and BuildBuddy providers are already installed; that
does not establish equivalent native providers or safe state adoption for
Forgejo, GitHub, Authentik, GitLab, or RouterOS. `gitlab-spike` and
`home-switch` require separate controller and credential analysis.

## Next steps

1. For the 13-block ESO review pool, trace each value to its consumers and remote
   credential, design an explicit adoption or rotation, and test the handoff.
   ESO Password generates new values; recreating an ExternalSecret can rotate a
   credential. Never assume a generator preserves the existing value.
2. Prototype Authentik Blueprint ownership on a bounded object set only after
   proving a least-privilege OAuth credential delivery path and a rollback.
3. Investigate `home-switch` initialization separately before treating its
   checked-in source as applied. Inspect private `thrive-scrape` source before
   proposing conversions there.

## Method

On 2026-11-23, `kubectl get workspaces.opentofu.upbound.io -o json` supplied
names, Ready conditions and inline modules. Resource declarations were counted
by type without printing module contents or credentials. The two tofu-controller
root counts come from `tf/gitops` at the rebased repository revision. Provider
readiness and native resource conditions were checked separately. A Ready
condition is not proof that each downstream consumer works. Recount against the
actual applied source and validate consumers before a migration.
