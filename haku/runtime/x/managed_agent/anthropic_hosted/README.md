# Managed Agents — Anthropic-hosted sandbox

Status: **PARKED (2026-07-04); Terraform root retired (2026-11-21).** The cloud control-plane
objects (environment/agent/vault/credentials/deployment) were deleted at Anthropic by the
operator on 2026-09-30. The OpenTofu root that provisioned them and its suspended
tofu-controller resource (`cluster/parked/cloud-agent-tf`) were then deleted, because their
provider is dormant ([why](#why-the-terraform-root-was-retired)). Nothing here is deployed.
Resuming needs a reviewed reactivation decision and starts from [Resuming](#resuming). The
sections from [Why cloud](#why-cloud-vs-the-self-hosted-worker) on describe the **v0
architecture as it was built**, before parking — historical until resumed.

- **Cluster access path:** the public, Authentik-authed `kubectl-machine-mcp`
  passthrough MCP (<../../../../../cluster/parked/kubectl-machine-mcp/README.md>).

> **Retired (2026-06-26):** the first imperative bring-up for this agent (`provision.sh` plus
> `haku.{agent,environment,deployment}.yaml`) and **Path B** (the cloud agent `curl`ing
> `kubeapi.allegedly.works` directly with a `KUBE_TOKEN` env-var vault credential). Path A
> replaced them: cluster access through `kubectl-machine-mcp` with a `static_bearer` credential
> bound to the MCP URL, so the token is never substituted into arbitrary egress. Terraform
> provisioned Path A until its root was deleted.

This doc is the **architecture + forward direction** beyond v0. Before parking,
the agent only ran a connectivity test (lists `haku-sandbox` pods); the
ephemeral-compute run loop below was never built.

## What the deleted root provisioned

Last tree with the root and its CR: `77cd80fa40`. Read them with
`git show 77cd80fa40:haku/runtime/x/managed_agent/anthropic_hosted/terraform/main.tf` and
`git show 77cd80fa40:cluster/parked/cloud-agent-tf/terraform.yaml`.

- **Environment** `haku-cloud`: `config.type = cloud`, `networking.type = unrestricted` (v0;
  narrowing to `limited` + `allowed_hosts` is in <TODO.md>).
- **Agent** `haku-cloud`: model, toolset and MCP URLs matched <../self_hosted/haku.agent.yaml>
  (a test enforced it): the `kubectl-machine` (`https://kubectl-machine-mcp.allegedly.works/mcp`)
  and `haku-console` (`https://haku.allegedly.works/mcp`) MCP servers, every toolset
  `always_allow`. Only the name, the `system` prose ("do exactly what each user message asks,
  then stop") and the environment differed.
- **Vault** `haku-cloud` with `static_bearer` credentials, each bound to one MCP URL:
  - `kubectl-machine`: the `haku-k8s` Authentik JWT, which the MCP forwards to kube-apiserver
    (`groups:["haku"]` → `oidc-ksbx-groups:haku` → `haku-sandbox-admin`). It rotates about every
    44 days; the write-only `token` used the JWT's `exp` as `token_wo_version`.
  - `haku-console`: the console's static Agent bearer. **Gone:** #11897 (2026-10-24) retired
    static Agent bearers and nothing creates that Secret now.
- **Deployment** `haku-cloud` (agent + environment + vault, `desired_status = active`), run on
  demand with `ant beta:deployments run`. Its only initial event was a connectivity test: list
  `haku-sandbox` pods.
- **Provider key:** `ANTHROPIC_API_KEY` in the tofu-controller runner, from a dedicated,
  spend-capped Anthropic workspace key (`llm-anthropic-haku`, shared with LiteLLM).
- **State:** `pg` backend schema `haku_cloud_agent` in `tofu-state-db-ovh`. Outputs (vault, agent,
  environment and deployment IDs) went to Secret `haku-cloud-agent-ids` in `flux-system`. The
  schema and Secret may still exist (unchecked); the IDs refer to deleted objects, so never
  consume them.

## Why the Terraform root was retired

Provider: `modus-agendi/anthropic-claude-managed-agents`, a community provider locked at 1.2.0.
Checked 2026-11-21:

- **Dormant.** v1.2.0 (tag 2026-07-10) is a version-only bump. `main` has no commit after
  2026-07-26, when the maintainer added a `.terraform-registry` file reading
  `Request: remove from registry` for the Terraform registry. Both registries still listed
  1.2.0 that day. Its 1.0.0 changelog says "Not production software".
- **No `self_hosted`.** `config.type` supports only `cloud`, and `networking` is `Required`
  but the API rejects it for `type=self_hosted`. This forced the self-hosted agent onto `ant`
  (PR #2780).
- **Deletions went unnoticed.** After the objects were deleted at Anthropic, tofu-controller
  kept reporting `Plan no changes`/Ready against phantom state; recovery needed
  `state rm`/`-replace`. Upstream `Read` drops state only on a 404 (all seven resources), so
  the API probably did not answer 404 for these deletions (unchecked).
- **Release assets.** The Bazel provider mirror dropped it because v1.1.0 no longer resolved
  through the GitHub release path the OpenTofu registry advertises. Not rechecked for 1.2.0.
- **Supply chain.** One maintainer, holding an org API key; each version bump needed a manual
  source-diff review of `internal/client/` (egress and secret handling) and
  `.github/workflows/release.yml`.

## Resuming

Recreate with `ant`, as <../self_hosted/provision.sh> does for the self-hosted agent: a fresh
environment, agent, vault, credentials and deployment, with model, toolset and MCP URLs copied
from <../self_hosted/haku.agent.yaml>. Never reuse IDs from `haku-cloud-agent-ids`. Also needed:
the parked `kubectl-machine-mcp`
(<../../../../../cluster/parked/kubectl-machine-mcp/README.md>), a `haku-k8s` JWT input (the
Authentik rotation no longer publishes a copy for this agent), and a new credential path for
`haku-console`.

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
  **full in-cluster reach** — Plaid, the in-cluster MCP servers, internal Forgejo — so we **don't** expose each
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
