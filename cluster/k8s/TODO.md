# Cluster K8s TODO

Audit findings deferred for later.

## Study Casino database backups

- [ ] Choose and deploy the backup/restore path for the CNPG
      `study-casino/study-casino-db` cluster. Schedule regular backups, keep
      enough retention to use the available storage comfortably, and run a
      restore drill before relying on the backups.

## Mitmproxy: forward in-cluster + label-selector fix (DRAFT, uncommitted)

The Kyverno `inject-mitmproxy` policy auto-injects `HTTP_PROXY` into every
pod in `claude-sandbox`, `openclaw-sandbox`, `openclaw-gateway`. Two issues:

1. The `ccnp-sandbox-proxy-egress` CCNP allowed traffic to pods labeled
   `app: mitmproxy` — but the deployment uses
   `app.kubernetes.io/name: mitmproxy`, so the rule matched no pods and
   sandbox pods couldn't reach mitmproxy when they needed to. External
   pip/curl through `HTTP_PROXY` silently timed out. **Real bug.**
2. Mitmproxy's egress (`cnp-cloud-api-egress`) only allowed a fixed set
   of cloud LLM FQDNs. So when a sandbox pod sent a request through
   mitmproxy targeting an in-cluster destination (e.g. an
   HTTPS_PROXY-respecting tool that ignored NO_PROXY), mitmproxy
   couldn't actually forward to `ollama.ollama`.

Drafted fix (uncommitted, in working tree): keep mitmproxy in-path and
required for sandbox pods, but extend its egress to allow forwarding to
in-cluster Services. Bench Jobs that want mitmproxy to forward don't need
code changes; existing NO_PROXY-aware tools still bypass for in-cluster
destinations as before.

Files touched:

- <../agents/mitmproxy/ccnp-sandbox-proxy-egress.yaml> —
  label-selector fix (the real bug).
- <../agents/mitmproxy/cnp-cloud-api-egress.yaml> — added
  `toEntities: cluster` egress rule on common ports (80, 443, 8000,
  8080, 11434) so mitmproxy can forward to in-cluster Services.

`NO_PROXY` in `inject-mitmproxy.yaml` is unchanged. Open question:
whether to also tighten `NO_PROXY` (forcing all sandbox traffic through
mitmproxy unconditionally) — that's a stricter posture giving full audit
but losing the bypass escape hatch.

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
it at a self-hosted option — either cluster Forgejo (`git.allegedly.works`) or a
self-hosted GitLab instance — so agent work can land in private repos without relying
on `github.com`. GitLab PAT just needs `GITLAB_TOKEN` env var (same pattern as
`GITHUB_TOKEN`); Forgejo would need a git-credential helper or embedded PAT in URLs
since there's no first-class Forgejo provider in OpenHands.

## OpenHands sandbox egress isolation

- [ ] Add NetworkPolicy/CiliumNetworkPolicy to `openhands-sandboxes` namespace blocking
      egress to internal cluster IP ranges (pod CIDR `10.244.0.0/16`, service CIDR
      `10.96.0.0/12`). Only allow egress to external internet + DNS (port 53 to
      kube-dns). Agent pods shouldn't reach internal cluster services by default.

## Missing CiliumNetworkPolicy

71% of namespaces lack network policies. Tracked in `cluster/docs/plan.md`.

**Proxy-mode services** missing the required NetworkPolicy restricting ingress
to the authentik shared proxy outpost pod (per AGENTS.md):
agents-mitmproxy, proxmox.

**Critical unprotected services** (no NetworkPolicy at all):
external-secrets, forgejo, harbor.

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
tana-mcp, mitmproxy, props, atuin.

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
means wyrm2-only (`rugged` is roaming/often NotReady, and the old
`talos-pve-cp-0` VM is retired). wyrm2 itself uses `cache.allegedly.works` during
`nixos-rebuild`. When wyrm2's `/nix/store` crosses the kubelet DiskPressure
threshold, kubelet adds `node.kubernetes.io/disk-pressure:NoSchedule`, attic +
postgres get evicted with nowhere to land, the gateway returns 503, and wyrm2
can't rebuild itself out of the situation.

Options to consider:

- [ ] Add a second `region=proxmox` node, or relax the selector to also accept
      `hil`.
- [ ] Move attic off wyrm2 entirely so wyrm2's local disk can't take the
      cache down with it.
- [ ] Configure a fallback substituter on wyrm2 (e.g. `cache.nixos.org`
      ahead of `cache.allegedly.works`, or as a fallback) so a degraded
      attic doesn't block local rebuilds.
