# Managed Agents — Anthropic-hosted sandbox

Status: **control plane shipped (v0).** Haku's cloud Managed Agent
(environment + agent + vault + static_bearer credential + deployment) is now
provisioned **declaratively via Terraform**, not the imperative `ant` scripts
this directory used to hold. The live wiring:

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

This doc is the **architecture + forward direction** beyond v0. The agent today
only runs a connectivity test (lists `haku-sandbox` pods); the ephemeral-compute
run loop below is not built yet — see <PLAN.md>.

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
  data-source MCP separately. Kyverno injects the `haku-mitmproxy` egress + RBAC +
  quota **by namespace**, so agent-created pods inherit the same fence (and PSS
  constrains what the agent can create — no privileged, runAsNonRoot).

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
