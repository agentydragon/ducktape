# Cluster K8s TODO

Audit findings deferred for later.

## Study Casino database backups

- [ ] Choose and deploy the backup/restore path for the CNPG
      `study-casino/study-casino-db` cluster. Schedule regular backups, keep
      enough retention to use the available storage comfortably, and run a
      restore drill before relying on the backups.

## Mitmproxy: tighten `NO_PROXY`?

The label-selector fix (`ccnp-sandbox-proxy-egress`) and the in-cluster
forwarding egress rule (`cnp-cloud-api-egress` `toEntities: cluster`) are
committed. `NO_PROXY` in `inject-mitmproxy.yaml` is unchanged.

- [ ] Decide whether to tighten `NO_PROXY` to force all sandbox traffic
      through mitmproxy unconditionally — a stricter posture giving full
      audit but losing the in-cluster bypass escape hatch.

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

## Event-driven autoscaling

- [ ] Consider deploying [KEDA](https://keda.sh/) for event-driven autoscaling and
      scale-to-zero workloads, especially queue-backed workers, scheduled jobs,
      and services whose load signals are not well represented by CPU/memory HPA.

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

The Grocy MCP server (`grocy-mcp-server` deployment, instantiated per site in
the `grocy-sf` and `grocy-vallejo` namespaces) crash-loops on first boot
until Grocy receives its first HTTP request (which triggers database migrations).
The MCP server tries to fetch Grocy's OpenAPI spec at startup, but Grocy returns
errors until migrations complete. After a manual visit to the Grocy web UI,
the MCP server starts successfully. Consider an init container or startup probe
that pokes Grocy's `/login` endpoint before the MCP server starts.

## Remove `kubectl-local` MCP server

Now that `cluster-kubectl-sandbox-diagnostics` (in-cluster OAuth MCP at
`kubectl-sandbox-mcp.allegedly.works`) is configured in `.mcp.json`, the local
`kubectl-local` MCP wrapper (`devinfra/claude/kubectl_local_mcp.py`) is
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

## Slim down `authentik-jwt-rotation` image

- [ ] 2026-06-18: Revisit `authentik-jwt-rotation` image contents. It still
      bundles shell tools like `curl`/`git`; consider moving to the standard
      Python image pattern with `pygit2` and certifi/CA-certs, matching the
      other pygit2-based images, and remove the extra package cruft.

## Gateway: restrict `cluster-gateway` `allowedRoutes` (no agent self-exposure)

- [ ] 2026-06-26: `cluster/k8s/gateway/gateway.yaml` listeners are all
      `allowedRoutes: namespaces: from: All`, so an HTTPRoute in **any** namespace
      can attach to the public gateway and bypass Authentik. Not currently
      exploitable by the agents — `haku-sandbox-admin` / `claude-sandbox-admin` are
      explicit resource allowlists that omit `httproutes`/`gateways` — but it's a
      latent hole: any future RBAC slip granting an agent-writable namespace
      `httproutes` would let it self-expose publicly. Tighten
      `allowedRoutes.namespaces` to a label `Selector` (or explicit set) that agent
      namespaces (`haku-sandbox`, `claude-sandbox`, …) never carry, so "the agent
      can't create public routes" holds structurally at the gateway, not only via
      RBAC. The agent-authored Haku UI (`haku/console/plans/free_form_ui_iframe.md`)
      depends on this — its UI must stay reachable only via the operator-owned
      Authentik route.
