# Managed Agents — Anthropic-hosted sandbox

Status: **PARKED (2026-07-04).** The cloud control-plane objects
(environment/agent/vault/static_bearer credential/deployment) were deleted at
Anthropic by the operator, and `cluster/k8s/haku/cloud-agent-tf` is suspended
(`suspend: true`). Resuming starts with the decision in
[Resuming](#resuming-recreate-via-the-provider-vs-retire-it) below. What follows
describes the **v0 architecture as it was built**, before parking — historical
until resumed:

- **Terraform root:** <../../../../tf/gitops/haku-cloud-agent> — the
  `claude-managed-agents` provider, pinned + hash-locked.
- **Deployed by:** <../../../../cluster/k8s/haku/cloud-agent-tf> (tofu-controller
  `Terraform` CR; runbook + provisioned IDs there).
- **Cluster access path:** the public, Authentik-authed `kubectl-machine-mcp`
  passthrough MCP (<../../../../cluster/k8s/agents/kubectl-machine-mcp/README.md>).

> **Retired (2026-06-26):** the imperative bring-up — `provision.sh` plus
> `haku.{agent,environment,deployment}.yaml` — and **Path B** (the cloud agent
> `curl`ing `kubeapi.allegedly.works` directly with a `KUBE_TOKEN` env-var vault
> credential). Terraform now owns provisioning, and cluster access goes through
> `kubectl-machine-mcp` with a `static_bearer` credential bound to the MCP URL
> (Path A), so the token is never substituted into arbitrary egress.

This doc is the **architecture + forward direction** beyond v0. Before parking,
the agent only ran a connectivity test (lists `haku-sandbox` pods); the
ephemeral-compute run loop below was never built.

## Resuming: recreate via the provider vs retire it

Before rebuilding, decide whether to keep using the `claude-managed-agents`
OpenTofu provider at all. It has bitten in three distinct ways:

1. **No `self_hosted` support** — `networking` is `Required` but the API rejects
   it for `type=self_hosted`, which forced the self-hosted agent off TF entirely
   (back to imperative `ant`, PR #2780).
2. **`Read` doesn't detect deletions** — after the objects were deleted,
   tofu-controller kept reporting `Plan no changes`/Ready against phantom state;
   recovery needs `state rm`/`-replace`, not a normal reconcile.
3. **Supply-chain caution** — a single-maintainer, low-download third party
   holding an org API key; every version bump needs a manual source-diff review.

**Option A — `state rm` + unsuspend + recreate via the provider.** Lowest
immediate effort; fresh IDs refresh the `haku-cloud-agent-ids` output Secret and
unblock the parked shared-vault cutover (PR #2788). Keeps GitOps declarative
reconciliation for the cloud agent — and all three liabilities, drift-blindness
included.

**Option B — retire the provider; manage the cloud agent imperatively**, as the
self-hosted agent already is (`provision.sh` + `ant`). Drops the provider, its
supply-chain surface, and the repin-review burden. Cost: the cloud agent loses
declarative reconciliation. Coupling: PR #2788's shared vault lives _in the TF
module_ and self-hosted reads its ID from the TF output Secret, so retiring the
provider also unwinds vault-in-TF — the shared vault moves to imperative
provisioning too. The SSOT/parity test (`//haku/base:test_agent_config_ssot`)
stays valid either way.

**Lean: B** — the provider has produced two silent-failure bugs and the
imperative path is proven for self-hosted — unless declarative GitOps for the
cloud agent specifically is worth the ongoing liability. Option A is acceptable
as a stopgap to get a working baseline first.

## Why cloud (vs the self-hosted worker)

Sibling of <../self_hosted/README.md>: same Managed Agents loop (server-side at
Anthropic), but the **sandbox runs in Anthropic's cloud**, not our cluster. The
self-hosted worker owned a class of runtime bugs (notably the empty-tool-result
deadlock, [anthropic-sdk-go#377](https://github.com/anthropics/anthropic-sdk-go/issues/377),
plus the whole image/closure/egress bring-up). Cloud sandboxes are operated by
Anthropic and don't hit those — at the cost of moving tool execution off our
infra.

## The problem cloud mode creates

The cloud sandbox's `bash`/files run on Anthropic infra and **cannot reach
anything cluster-internal** — Plaid Postgres, in-cluster MCP servers, `kubectl`
via the `haku` SA, `git.allegedly.works`-internal. Everything Haku touches must
be reachable from Anthropic's side.

## Architecture: one MCP + ephemeral in-cluster compute

Rather than expose every data-source MCP server, expose **one** thing and let
Haku spin up its own compute _inside_ the perimeter:

- **The `haku`-scoped `kubectl-machine-mcp` passthrough MCP** (shipped). The cloud
  sandbox connects to it; the `static_bearer` credential carries the `haku-k8s`
  Authentik JWT, which the MCP forwards to kube-apiserver
  (`groups=[haku]` → `oidc-ksbx-groups:haku` → `haku-sandbox-admin`, full CRUD in
  `haku-sandbox`). Exposes `pods_run` / `resources_create_or_update` / `pods_exec`
  / `pods_delete` (no `read_only`).
- **Ephemeral compute pods** (not built). Per wake, Haku
  `resources_create_or_update`s a pod in `haku-sandbox` (a trivial tools image —
  git/kubectl/psql/curl/cacert, **no `ant`, no systemd**) with the `haku` SA + git
  creds, `exec`s the scan into it, then `delete`s it. A pod in `haku-sandbox` has
  **full in-cluster reach** — Plaid, the in-cluster MCP servers, the
  `google-access-token` secret, internal Forgejo — so we **don't** expose each
  data-source MCP separately. Kyverno injects the `haku-egress-proxy` egress + RBAC +
  quota **by namespace**, so agent-created pods inherit the same fence (and PSS
  constrains what the agent can create — no privileged, runAsNonRoot).

Secrets the pod uses **locally** — git creds for the `haku-state` clone, possibly
the SOPS age key — must be in-cluster k8s Secrets mounted into the pod, not vault
`environment_variable` credentials: vault env-var substitution is **egress-only**,
so anything consuming a secret in-pod sees the opaque placeholder, not the real
value. Vault credentials are only for the MCP bearer (and any
verbatim-in-outbound-request API keys).

Net: **Anthropic cloud brain + one `haku`-scoped k8s MCP + a trivial tools
image.** Arbitrary in-cluster compute still runs in `haku-sandbox` behind the same
perimeter; credentials stay in-cluster (the pod SA; vault-injected MCP creds).

## Tradeoffs vs self-hosted

- **+** No agent-execution-runtime bugs (the whole `self_hosted/debug/` chain
  vanishes); Anthropic operates the hands; tighter tool surface (curated MCP, not
  broad bash+kubectl+psql).
- **−** Per-wake pod-creation latency; the cloud-vs-in-cluster filesystem split
  (do real work _inside_ the pod via `exec`, treat the cloud sandbox as
  orchestration glue); more moving parts (MCP + RBAC), though each is dumber and
  well-trodden.
- **=** Data exposure is unchanged — tool inputs/outputs already flow to
  Anthropic's control plane even when self-hosted.

## Open design points

- `pods_run`'s image+command may be too thin for SA + secret env — likely need
  `resources_create_or_update` with a small pod manifest.
- A `pods_exec`-capable MCP = RCE into `haku-sandbox` (same blast radius as
  today's worker); the Authentik/RBAC scoping on the `haku-k8s` token is the
  fence — keep it tight.
- Whether to keep `self_hosted/` as a fallback or tombstone it once this lands.
