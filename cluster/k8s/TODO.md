# Cluster K8s TODO

Audit findings deferred for later.

## Study Casino PVC backups

- [ ] Choose and deploy a cluster PVC backup solution, then enable backups for
      `study-casino/study-casino-data` every 3 hours. Preserve `/data`
      SQLite state (`casino-*.db`, WAL/SHM files when present), keep enough
      retention to use the available storage comfortably, and include a tested
      restore path before relying on it.

## Mitmproxy / claude-sandbox: design a clean egress story

**Current state is broken for `claude-sandbox`.** Every pod created in
`claude-sandbox` is stuck `ContainerCreating` with
`MountVolume.SetUp failed for volume "mitmproxy-ca-cert" : configmap
"mitmproxy-ca-cert" not found`, because:

1. The Kyverno `inject-mitmproxy` ClusterPolicy
   (<../k8s/kyverno/policies/inject-mitmproxy.yaml>) injects a
   `mitmproxy-ca-cert` ConfigMap volume + `HTTP{S,}_PROXY` env vars into
   every pod in `claude-sandbox`, `openclaw-sandbox`, `openclaw-gateway`.
2. The source ConfigMap lives in the `openclaw-mitmproxy` namespace
   (<../k8s/agents/openclaw/mitmproxy/kustomization.yaml>) and is meant
   to be replicated into `claude-sandbox` via Reflector annotations.
3. But the `openclaw-mitmproxy` Flux Kustomization is currently
   `suspend: true` (dependency `openclaw-mitmproxy-namespace` is not
   ready), so the source ConfigMap never gets generated → Reflector has
   nothing to copy → no pods in `claude-sandbox` can start.

So today the choice is implicit: either no claude-sandbox pods, or
resurrect the openclaw-mitmproxy stack.

**Goal (Rai, 2026-05-17): no unrestricted internet for sandbox pods,
but reaching common in-cluster services should "just work" without
each consumer threading manual `NO_PROXY` config.**

Open design questions:

- Should mitmproxy stay in-path for claude-sandbox at all, or do we
  prefer a pure CiliumNetworkPolicy egress allowlist (no proxy, no env
  injection, no CA cert) that permits a curated list of in-cluster
  Services + denies everything else? The latter is simpler to reason
  about and removes the "missing CM → pod stuck" failure mode entirely.
- If we keep mitmproxy: fix the two upstream bugs first
  (label-selector mismatch on `ccnp-sandbox-proxy-egress`, missing
  `toEntities: cluster` on `cnp-cloud-api-egress`), un-suspend the
  Flux Kustomization, and make the in-cluster forward path the
  default. Drafted patches live in
  <../k8s/agents/openclaw/mitmproxy/PROBLEM.md>.
- Either way, the Kyverno injection policy should fail-closed
  (refuse pod admission) when the ConfigMap it's about to mount
  doesn't exist, rather than mutating pods into a permanent
  ContainerCreating state. Could be a CEL/validating policy that
  short-circuits when `mitmproxy-ca-cert` isn't reflected yet.

Concrete next steps (any order):

- [ ] Audit which claude-sandbox use cases need external internet vs
      cluster-only. (LLM API access from agent pods? GitHub clones?
      package fetches? cluster Service consumption only?)
- [ ] Decide between "mitmproxy in-path" vs "CiliumNetworkPolicy
      allowlist only" for claude-sandbox. Document the choice.
- [ ] If mitmproxy stays: land the two fixes in
      `agents/openclaw/mitmproxy/`, un-suspend the kustomization,
      verify CA cert reflects to claude-sandbox.
- [ ] If we drop mitmproxy for claude-sandbox: scope the Kyverno
      policy to remove `claude-sandbox` from the `match.namespaces`
      list (<../k8s/kyverno/policies/inject-mitmproxy.yaml>), and
      replace it with a `CiliumNetworkPolicy` in
      `agents/claude-rbac/` that allowlists the needed cluster
      Services.
- [ ] Add a guard so the Kyverno policy can't leave pods stuck in
      ContainerCreating when its dependencies aren't present.

## OpenClaw secrets

- [ ] `agents/openclaw/sandbox-secrets/ibkr-flex-query-credentials.sops.yaml` — consider moving `query-id` out of SOPS (not sensitive)

## Mobile Nebula phone followups

- [ ] Test cluster-hosted ActivityWatch from the phone over Nebula, either by
      browser/API against `activitywatch.nebula.allegedly.works` or direct
      `10.42.0.40` if Android/Mobile Nebula DNS behavior is awkward.
- [ ] Allow SSH from the phone to mesh machines such as `wyrm2`; verify the
      phone has an SSH client/key path, host SSH/firewall policy allows the
      phone's Nebula identity or `10.42.0.50`, and access stays key-only.
- [ ] Consider running an SSH daemon on the phone for emergency access back to
      the device, probably via Termux/OpenSSH, with explicit keys and a clear
      power/background-execution story.

## OpenHands: self-hosted git provider

OpenHands supports GitHub and GitLab natively (PAT or OAuth App). Consider pointing
it at a self-hosted option — either cluster Gitea (`git.allegedly.works`) or a
self-hosted GitLab instance — so agent work can land in private repos without relying
on `github.com`. GitLab PAT just needs `GITLAB_TOKEN` env var (same pattern as
`GITHUB_TOKEN`); Gitea would need a git-credential helper or embedded PAT in URLs
since there's no first-class Gitea provider in OpenHands.

## OpenHands sandbox egress isolation

- [ ] Add NetworkPolicy/CiliumNetworkPolicy to `openhands-sandboxes` namespace blocking
      egress to internal cluster IP ranges (pod CIDR `10.244.0.0/16`, service CIDR
      `10.96.0.0/12`). Only allow egress to external internet + DNS (port 53 to
      kube-dns). Agent pods shouldn't reach internal cluster services by default.

## Missing CiliumNetworkPolicy

71% of namespaces lack network policies. Tracked in `cluster/docs/plan.md`.

**Proxy-mode services** missing the required NetworkPolicy restricting ingress
to the authentik shared proxy outpost pod (per AGENTS.md):
openclaw-mitmproxy, proxmox.

**Critical unprotected services** (no NetworkPolicy at all):
external-secrets, gitea, harbor.

## Missing resource limits

Goldilocks VPA enabled (auto mode) for nix-cache, ollama, and litellm —
will recommend limits.

- `nix-cache/app/deployment.yaml` — attic container missing `resources:`
- `ollama/app/deployment.yaml` — auth-proxy sidecar missing `resources:`
- `litellm/app/deployment.yaml` — litellm container missing `resources:`

## SecurityContext

Talos enforces `baseline` Pod Security Standards by default via PSA.
Explicit `securityContext` on Deployments is defense-in-depth. Low urgency.

Missing securityContext: litellm, ollama, devbot, grocy-sf, grocy-vallejo, proxmox-proxy,
tana-mcp, openclaw/mitmproxy, props, atuin.

## Grocy MCP startup probe

The MCP servers (grocy-mcp-sf, grocy-mcp-vallejo) crash-loop on first boot
until Grocy receives its first HTTP request (which triggers database migrations).
The MCP server tries to fetch Grocy's OpenAPI spec at startup, but Grocy returns
errors until migrations complete. After a manual visit to the Grocy web UI,
the MCP server starts successfully. Consider an init container or startup probe
that pokes Grocy's `/login` endpoint before the MCP server starts.

## Remove `kubectl-local` shell-script MCP server

Now that `cluster-kubectl-sandbox-diagnostics` (in-cluster OAuth MCP at
`kubectl-sandbox-mcp.allegedly.works`) is configured in `.mcp.json`, the local
`kubectl-local` shell-script wrapper (`devinfra/claude/kubectl-local-mcp.sh`) is
likely redundant — both resolve to the same sandbox-scoped RBAC.

Before removing:

- [ ] Verify that the in-cluster OAuth MCP server works from Claude Code **web**
      (currently blocked: claude.ai OAuth redirect mismatch prevents auth against
      the in-cluster server; plan is to configure the URL as an MCP server in
      claude.ai and have Claude Code web inherit it)
- [ ] Once web works, remove `kubectl-local` from `.mcp.json` and update CLAUDE.md
      references to `kubectl-local`

## `ghcr.io/servercontainers/samba:latest`

No semver tags published. Keep `:latest` until upstream adopts versioned releases.

## Nix cache circular dependency on wyrm2 (incident 2026-05-06)

`attic` and its CNPG postgres are pinned to `region=proxmox`, which in practice
means wyrm2-only (`talos-pve-cp-0` has the control-plane taint, `rugged` is
roaming/often NotReady). wyrm2 itself uses `cache.allegedly.works` during
`nixos-rebuild`. When wyrm2's `/nix/store` crosses the kubelet DiskPressure
threshold, kubelet adds `node.kubernetes.io/disk-pressure:NoSchedule`, attic +
postgres get evicted with nowhere to land, the gateway returns 503, and wyrm2
can't rebuild itself out of the situation.

Options to consider:

- [ ] Add a second `region=proxmox` node (e.g. tolerate the control-plane
      taint on `talos-pve-cp-0`, or relax the selector to also accept `hil`).
- [ ] Move attic off wyrm2 entirely so wyrm2's local disk can't take the
      cache down with it.
- [ ] Configure a fallback substituter on wyrm2 (e.g. `cache.nixos.org`
      ahead of `cache.allegedly.works`, or as a fallback) so a degraded
      attic doesn't block local rebuilds.
