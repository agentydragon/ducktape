# Build plan — Managed Agents, Anthropic-hosted sandbox

Status: **PARKED (2026-07-04)** — the cloud control-plane objects
(env/agent/vault/credential/deployment) were **deleted at Anthropic by the operator**, and
`cluster/k8s/haku/cloud-agent-tf` is now `suspend: true` (PR #2790). Resuming this plan means
first resolving the decision in [Resuming: recreate-via-provider vs retire the
provider](#resuming-recreate-via-provider-vs-retire-the-provider) below. The P0/P1–P3 build
notes that follow are **historical** (they describe the pre-deletion Terraform path).

_Historical:_ **P0 shipped** (control plane provisioned via Terraform; the agent runs a
connectivity test). Architecture + rationale are in <README.md>; this is the
actionable build/test plan for what's left (P1–P3). Delete or tombstone once the
full run loop is running.

## Resuming: recreate-via-provider vs retire the provider

Before rebuilding the cloud agent, decide **whether to keep using the
`claude-managed-agents` OpenTofu provider at all.** It has now bitten in three distinct ways:

1. **No `self_hosted` support** — forced the self-hosted agent off TF entirely (reverted to
   imperative `ant`, PR #2780); `networking` is `Required` but the API rejects it for
   `type=self_hosted`.
2. **`Read` doesn't detect deletions** — after the objects were deleted, tofu-controller kept
   reporting `Plan no changes` / Ready against phantom state. Recovery needs `state rm`/`-replace`,
   not a normal reconcile.
3. **Supply-chain caution** — single-maintainer, low-download third party holding an org API key;
   every version bump needs a manual source-diff review (we are not the maintainer).

**Option A — unsuspend + recreate via the provider.** Lowest immediate effort: `state rm` the
phantom resources, unsuspend, let it re-apply (fresh env/agent/vault/deployment IDs → the
`haku-cloud-agent-ids` output Secret refreshes → the parked shared-vault cutover from PR #2788
becomes possible). Keeps GitOps declarative reconciliation for the cloud agent, but keeps all
three liabilities above, and the drift-blindness means future silent breakage is likely.

**Option B — retire the provider; manage the cloud agent imperatively too.** The self-hosted
agent already runs this way (`provision.sh` + `ant`). Doing the same for cloud drops the provider,
its supply-chain surface, and the repin-review burden, and makes both agents consistent. Cost:
the cloud agent loses GitOps declarative reconciliation (same trade the self-hosted one already
made). **Coupling to note:** PR #2788's shared vault lives _in the TF module_ and self-hosted
reads its ID from the TF output Secret — so retiring the provider also unwinds the "vault in TF"
decision; the shared vault would move to imperative provisioning (or a small SDK-based tool),
and the SSOT/parity test (`//haku/base:test_agent_config_ssot`) stays valid regardless.

**Lean:** given the provider has produced two _silent-failure_ bugs and we've already proven the
imperative path works for self-hosted, **B is the stronger long-term bet** unless declarative
GitOps for the cloud agent specifically is worth the ongoing liability. Next step is a short spike,
not a rewrite: `state rm` + unsuspend to get a working baseline (Option A as a stopgap), then
evaluate porting cloud to imperative. See the ACMA provider caution + the deleted-object IDs in
the session memory.

Goal: run Haku on a **cloud** Managed Agents sandbox (Anthropic operates the
hands), reaching the cluster through the public, Authentik-authed
`kubectl-machine-mcp` — spinning **ephemeral pods** in `haku-sandbox` for
in-cluster compute. This sidesteps the self-hosted worker entirely (incl. the
empty-result deadlock,
[anthropic-sdk-go#377](https://github.com/anthropics/anthropic-sdk-go/issues/377)).

## What already exists (reuse, don't build)

- **`kubectl-machine-mcp`** — the public passthrough MCP that trusts the
  `haku-k8s` machine issuer (`cluster_auth_mode = passthrough`, so RBAC follows
  the token's group; full tool surface `pods_run`/`pods_exec`/`resources_create_or_update`,
  no `read_only`). See <../../../../cluster/k8s/agents/kubectl-machine-mcp/README.md>.
- **`haku` identity + RBAC**: group `haku` → `haku-sandbox-admin` (full CRUD in
  `haku-sandbox`); the `haku-k8s` machine principal
  (kubectl-sandbox-client-credentials → `groups=[haku]`) is the haku-scoped token
  path. See <../../../../cluster/k8s/agents/agent-rbac-base/README.md>.
- **The cloud control plane** (env/agent/vault/credential/deployment), managed by
  Terraform: <../../../../tf/gitops/haku-cloud-agent>, deployed by
  <../../../../cluster/k8s/haku/cloud-agent-tf>. The static_bearer credential's
  token rotation is fully automatic (rotator → k8s Secret → Flux → tofu → vault).
- Data-source MCP servers (google/plaid/postscanmail/manifold/tana) deployed.

## How the vault→MCP credential binding works

Vault MCP credentials are **keyed by `mcp_server_url`**; when the agent connects
to that URL, Anthropic injects the bearer. Haku uses **`static_bearer`** (a fixed
token, like Tana) bound to the `kubectl-machine-mcp` URL: `kubectl-machine-mcp`
(passthrough) validates the JWT via JWKS + the `groups` claim and forwards it to
kube-apiserver. The token rotates ~every 44 days; the chain (rotator mints →
`haku-cloud-kube-token` Secret → Flux → tofu reads it in-cluster → re-sends to the
vault as a TF 1.11 write-only attribute) re-sends on each rotation with no manual
step. (`mcp_oauth` — Anthropic-refreshed `access_token`+`refresh_token` — is the
alternative the provider also models; we chose static_bearer since we own the
rotation cadence.)

### Secrets for the ephemeral pod (not vault env-vars)

The pod needs git creds (clone `haku-state`) and possibly the **SOPS age key**.
These are **in-cluster k8s secrets mounted into the pod** — _not_ vault
`environment_variable` credentials. Vault env-var substitution is **egress-only**:
anything that uses the secret locally (SOPS age decryption, signature
computation) sees the opaque placeholder, not the real value. So local-use
secrets stay in-cluster on the pod; vault credentials are only for the MCP bearer
(and any verbatim-in-outbound-request API keys).

## Phases (de-risk the linchpin first)

### P0 — cloud session reaches the cluster as `haku` — **DONE (2026-06-26)**

Shipped via Terraform (Path A): a `type: cloud` env (unrestricted egress for v0),
a `static_bearer` vault credential carrying the `haku-k8s` JWT bound to
`kubectl-machine-mcp`, and an agent with the `mcp_toolset` for that MCP. The
deployment's v0 initial event lists `haku-sandbox` pods — a connectivity test.
✓ proves cloud egress + the MCP→kube-apiserver passthrough + `haku` RBAC scope.

> Historical: the very first spike (2026-06-25) used **Path B** — the agent
> `curl`ed `kubeapi.allegedly.works` directly with a `KUBE_TOKEN` env-var
> credential, because the `haku-k8s` token's `aud` was rejected by the _shared_
> MCP. Path A replaced it once `kubectl-machine-mcp` (which trusts that issuer)
> shipped; Path B and its `provision.sh` bring-up are retired (see README).

### P1 — ephemeral compute

- Agent `resources_create_or_update`s a pod in `haku-sandbox` (a trivial tools
  image — git/kubectl/psql/curl; or reuse a toolbox image), `pods_exec`s
  `echo hi`, then `pods_delete`s it.
- **Pass =** clean create→exec→delete; output returned. Confirms write tools work
  under haku RBAC and PSS/Kyverno admit the pod.
- Decide: `pods_run` vs `resources_create_or_update` (SA + secret env likely needs
  the full manifest); settle the **pod template**.

### P2 — full scan

- The pod clones `haku-state` (git creds via mounted secret), scans **one** source
  (e.g. the Gmail token + a simple query), writes a finding, commits + pushes
  `haku-state`, exits.
- Author the **cloud run procedure** (a variant of haku-state's `memory/procedures/run.md`,
  or a Skill):
  "create pod → exec scan → commit haku-state → delete." Mind the cloud-sandbox vs
  in-pod **filesystem split** — do real work in the pod via `exec`.
- Decide **memory**: managed Memory (cloud-only) vs git `haku-state` in-pod.
- **Pass =** a wake produces a real `haku-state` commit, no manual steps.

### P3 — schedule + soak

- Scheduled deployment (replace the v0 on-demand `initial_events` with a
  `schedule` in main.tf). Run for a few days; compare cost, latency, reliability
  against `self_hosted`. Then decide whether to tombstone `self_hosted/`.

## Artifacts to land (P1–P3)

- The agent's `system` prompt + `mcp_servers`/`tools` evolve in
  <../../../../tf/gitops/haku-cloud-agent/main.tf> (no imperative `ant` YAMLs).
- A tools image (or reuse) + the pod template.
- Cloud run procedure (edits to haku-state's `memory/procedures/run.md`, or a Skill).

## Open unknowns / risks

- Per-wake pod-creation latency (acceptable for a background scanner; measure P3).
- `pods_exec` over a public MCP = RCE into `haku-sandbox` — same blast radius as
  today's worker, but the Authentik/RBAC scoping is the fence; keep it tight.
- Tighten cloud egress from `unrestricted` to `limited` + an explicit
  `allowed_hosts` once the data-source set is settled (TODO in main.tf).

## Workflow

Worktree + PR per phase.
