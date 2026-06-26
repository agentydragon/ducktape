# Managed Agents — Anthropic-hosted cloud TODO

P0 passed (cloud session reaches `kubeapi.allegedly.works` as `haku`); the phased
build plan is <PLAN.md>. Open items from the v0 bring-up:

- **Auto-propagate the rotated k8s token into the Anthropic vault.** `provision.sh`
  injects `KUBE_TOKEN` (from `secrets/haku-k8s-jwt.yaml`) as a one-shot vault
  credential. `authentik-jwt-rotation` rotates the in-cluster secret but does
  **not** push the refreshed token to the vault, so the cloud credential silently
  expires → kube-apiserver 401s with no pod touched. Interim: extend the rotation
  CronJob to `ant beta:vaults:credentials update` before expiry. The IaC
  alternative (manage the agent/vault with a Terraform provider) is evaluated
  below.
- **Tighten cloud egress.** `haku.environment.yaml` has `networking.type:
unrestricted` (TODO in-file). Narrow to `type: limited` + an explicit
  `allowed_hosts`. (The `KUBE_TOKEN` secret is already scoped — only substituted
  on `kubeapi.allegedly.works` — so this is hardening, not a leak fix.)
- **Move off Path B to the k8s-MCP path.** v0 `curl`s kube-apiserver directly
  with the `aud=kubectl-sandbox-client-credentials` token. The cleaner
  `kubectl-sandbox-mcp` path needs a token with `aud=kubectl-sandbox-mcp` +
  `groups=[haku]`, which no Authentik provider mints yet (<PLAN.md> P0 gate).

The Sonnet pin (`model: claude-sonnet-4-6`) is intentional for bring-up test runs
— not a TODO; revisit the model once the runtime is past v0.

## Terraform provider options (the IaC alternative for token propagation)

Managing the agent + vault credential as IaC would resolve the token-propagation
item cleanly. There is **no official/verified Anthropic provider** — all options
are `community` tier (surveyed 2026-06-25):

| Provider                                     | Scope                                                                   | Adoption                                  | Verdict                                                         |
| -------------------------------------------- | ----------------------------------------------------------------------- | ----------------------------------------- | --------------------------------------------------------------- |
| **`andasv/anthropic-claude-managed-agents`** | Managed Agents: agents, environments, **vaults**, skill uploads, memory | 2★, created 2026-05-12, no registry stats | Only one that fits — but immature; single author, no real users |
| `ippontech/anthropic`                        | Admin API (workspaces / API keys / members) — **not** agents/vaults     | ~8.5k downloads, 10★, active              | Doesn't cover our use case                                      |
| `jianyuan/anthropic`                         | Admin API — **not** agents/vaults                                       | ~1.7k downloads, oldest (since 2024-12)   | Doesn't cover our use case                                      |
| `gszzzzzz/claude`                            | Claude Admin API — **not** agents/vaults                                | ~1.3k downloads, 0★, stale (push 2026-03) | Doesn't cover our use case                                      |

Only `andasv/anthropic-claude-managed-agents` models `vaults` + credentials (the
resource we'd need), and it's too young to trust for Haku's control plane (uses
TF ≥1.11 write-only attributes for secrets, which is the right shape).

**Decision:** stay with scripting `ant beta:vaults:credentials update` from the
`authentik-jwt-rotation` CronJob for now; re-evaluate `andasv` once it has real
adoption.
