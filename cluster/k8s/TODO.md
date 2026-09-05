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

## Secret layout

- [ ] Restructure the flat agent token files under `secrets/` into clearer
      ownership folders (for example per-agent or per-rotator). Current
      rotators write paths like `secrets/*-k8s-jwt.yaml` and
      `secrets/*-forgejo-tea-token.yaml`; keep `.sops.yaml`, home-manager
      modules, and rotator configs in sync when moving them.

## InvenTree secrets if unsuspending

- [ ] Add SOPS secrets for InvenTree admin and database passwords before
      unsuspending it. The old Vault `kv/inventree/*` values are gone with
      Vault (decommissioned 2026-04-19; <../docs/decisions.md> § "Secrets: SOPS
      SSOT"), so generate fresh values.

## Mobile Nebula phone followups

- [ ] Wire the phone into ActivityWatch: a phone-specific exporter/importer
      feeding the bearer-gated write route with the same idempotent,
      provenance-preserving semantics as the desktop importer
      (<../docs/activitywatch/README.md>).
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

## Proxmox drift watch

`cluster/k8s/infra-drift/` plans the OVH half of `cluster/terraform/main` and
deliberately leaves the Proxmox resources out: the `proxmox` provider
authenticates as `root@pam!tofu`, a full-root API token, and putting that in a
tf-runner pod is a bigger step than the scoped OVH credentials the CR already
needs.

- [ ] Decide whether a narrower Proxmox principal can cover a plan-only
      refresh. `terraform@pve` with the `TerraformAdmin` role already exists in
      `persistent-auth.tf`; a read-only role (`VM.Audit`, `Datastore.Audit`,
      `Sys.Audit`) would be narrower still, but it has to be able to refresh
      `proxmox_virtual_environment_{vm,role,user,user_token}`.
- [ ] Then extend `infra-drift`'s `spec.targets` with the two VMs and the
      persistent role/user/token, and plant the token as
      `infra-drift-proxmox-token`. `proxmox_virtual_environment_vm.wyrm2` is
      the highest-value target — `proxmox-vms.tf` says its PCI passthrough is
      applied by hand with `qm` and the file "keeps TF in sync", which is
      precisely the divergence nothing watches.
- [ ] Two unknowns to settle when doing it: the provider declares
      `ssh { agent = true }`, and whether it tolerates a missing
      `SSH_AUTH_SOCK` at configure time is untested; and pin
      `rebuild_image = false` in the CR's `vars`, since `module.wyrm2_image` is
      a dependency of the wyrm2 VM and `true` puts an SSH-to-Proxmox check and
      a `nix build` in the graph.

## Missing CiliumNetworkPolicy

71% of namespaces lack network policies. Tracked in `cluster/docs/plan.md`.

**Proxy-mode services** missing the required NetworkPolicy restricting ingress
to the authentik shared proxy outpost pod (per AGENTS.md):
agents-mitmproxy, proxmox.

**Critical unprotected services** (no NetworkPolicy at all):
external-secrets, forgejo.

## VPA memory audit — are these limits really needed?

Goldilocks VPA was moved to `controlledValues: RequestsOnly` after the
2026-08-09 LiteLLM outage (VPA scales limits in proportion to requests, which
collapsed the CPU limit to 150m and made cold start impossible). Limits are now
whatever the manifest declares. Goldilocks has no cluster-level setting for
this, so the `default-vpa-requests-only` Kyverno policy defaults the annotation
on every namespace labelled `vpa-update-mode: auto`.

Gotcha: that policy adds the annotation only when it is **absent**, so a
namespace declaring any policy of its own — even a partial one that sets only
`minAllowed` — opts out of the default entirely and must spell out
`controlledValues` itself (`airlock`, `langfuse`).

That exposed a second problem: **14 containers declared a memory limit below
what they actually use**, and only VPA silently raising the ceiling kept them
alive. Each was raised to clear VPA's observed upper bound, tagged
`TODO(vpa-memory-audit)` at the site. Raising them was the safe move, not
necessarily the right one — several of these numbers look like leaks, not
working sets.

Worth investigating before treating the new ceilings as correct, roughly in
descending order of "that can't be right":

- [ ] `haku-openclaw-spike` openclaw — investigate the provisional 16Gi limit;
      an earlier VPA observation reported unusually high resident memory for a
      single OpenClaw process. Most suspicious number in the sweep.
- [ ] `forgejo` — 2Gi → 8Gi, VPA saw 3.24Gi. Git pack cache? Mirror sync?
- [ ] `haku-console` server — 512Mi → 3Gi, VPA saw 1.29Gi for a static-file +
      API server.
- [ ] `haku-egress-proxy` mitmproxy — 1Gi → 3Gi, VPA saw 1.15Gi. Second time it
      has outgrown its limit; the existing comment claims the working set stays
      bounded under `stream_large_bodies`, and it evidently does not.
- [ ] `grocy-{sf,vallejo}` valkey — 128Mi → 384Mi, VPA saw 256Mi. Check the
      eviction policy; this is a cache with apparently unbounded key growth.
- [ ] `website` nginx — 64Mi → 192Mi, VPA saw 100Mi to serve static files.
- [ ] Remaining, smaller: `grocy` mcp-base server (256Mi → 768Mi), `tana-mcp`
      firebase-resigner (128Mi → 256Mi), `tana-mcp-facade` server (256Mi →
      512Mi), `props` (1Gi → 1.5Gi), `activitywatch` readonly-proxy (64Mi →
      128Mi).

Follow-up on the mechanism itself:

- [ ] Add a `cluster/validation` check that rejects unknown
      `goldilocks.fairwinds.com/*` annotation keys. `cpu-min` was invented
      twice (`litellm` #3856, `langfuse`), goldilocks ignored it silently both
      times, and the litellm one was only discovered by the outage it failed to
      prevent.

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

## Gateway: `allowedRoutes` Selector (belt-and-suspenders; deferred)

Agent self-exposure — an HTTPRoute in any namespace attaching to the public gateway and
bypassing Authentik (`cluster/k8s/gateway/gateway.yaml` listeners are all `allowedRoutes:
namespaces: from: All`) — is **already fenced**: the `restrict-agent-gateway-routes`
Kyverno ClusterPolicy denies route/Gateway creation in the agent namespaces, and
`haku-sandbox-admin`/`claude-sandbox-admin` omit `httproutes`/`gateways` anyway. So the
hole the agent-authored Haku UI (`haku/console/docs/containment.md`) relies on
staying closed is closed. What's left is the gateway-layer belt-and-suspenders:

- [ ] **Still want the `allowedRoutes` Selector eventually** (at the gateway layer
      itself, not only via a per-namespace deny). Deferred because:
      (a) Cilium bug #42159 — per-listener `allowedRoutes` bleeds across listeners in
      this setup (see `cluster/docs/plan.md`); (b) the kube-API routes live in the
      built-in `default` namespace (and a flux-system route) with no in-repo `Namespace`
      manifest to carry a selector label. Revisit if/when #42159 is fixed and there's a
      clean way to label those built-in namespaces — then switch listeners to
      `from: Selector` and the denylist becomes redundant.

## Haku `haku-ui` / workloads pipe — hardening follow-ups

The `cluster/k8s/haku/workloads/` Flux pipe and the `haku-ui.allegedly.works`
Authentik route work; these tighten them (operator-approved as follow-ups):

- [ ] **Read-only deploy key for the `haku-state` GitRepository.** The pipe's
      `GitRepository` reuses the r/w `haku-forgejo-git` Secret for basic auth
      (Flux only pulls, but the cred is read/write — there's no separate read
      principal on the repo). Mint a read-only Forgejo deploy key for `haku-state`
      and point the GitRepository's `secretRef` at it instead.

- [ ] **Make tofu-controller recover runner restarts without a controller restart.**
      On 2026-07-11, `wyrm2` rebooted while `Terraform/flux-system/haku-state` was
      reconciling. The `haku-state-tf-runner` container restarted (exit 255), but
      tofu-controller kept the old reconciliation in `Initializing`, pinned to
      source revision `a980d1a7` even after `GitRepository/flux-system` advanced.
      This left the parent Kustomization and five Terraform dependents blocked
      until tofu-controller itself was restarted; deleting the runner and annotating
      the CR did not recover it. Track and help land upstream PR
      `flux-iac/tofu-controller#1838` (runner-RPC deadline plus failed-pod reaping),
      then upgrade when a release contains it. If upstream stalls, carry the patch
      in an operator-owned image or add a watchdog that restarts the controller when
      initialization makes no progress past a bounded deadline. Add an automated
      failure-injection test covering node loss/container restart during init. Full
      RCA: `cluster/docs/lessons_learned/2026_07_03_tofu_controller_runner_rpc_hang.md`.

- [ ] **Shorten tofu-state PostgreSQL dead-client detection.** A `wyrm2` reboot left an
      idle PostgreSQL backend session holding the `sso_providers` advisory lock even
      though its runner pod/IP was gone. PostgreSQL inherited the kernel's two-hour
      `tcp_keepalive_time`, so this did not recover promptly and blocked the Flux chain
      through `haku-console`. Configure shorter keepalives (and consider a bounded
      Terraform `tfstate.lockTimeout` to reduce retry churn), then failure-test runner
      node loss. RCA and recovery matrix:
      `cluster/docs/lessons_learned/2026_07_11_tofu_pg_orphaned_session_lock.md`.

## Ship node logs to Loki (not just pod logs)

Motivation: Alloy/promtail scrape pod logs only, so kernel/service messages — including
the RTX 5090 `Xid 79 "GPU has fallen off the bus"` events — never leave a node's local
journal, leaving no cluster-side history to quantify GPU fall-off frequency. Broader
detect/quantify plan: <../../debug/atlas/gpu_lockup_20260718_followups.md>.

- [ ] **Unblock clean full OpenTofu plans.** Targeted Talos plans converge, but a
      no-target `tofu plan` still stalls during provider refresh even after regenerating the
      ignored `terraform/main/kubeconfig` with `https://api.allegedly.works:6443`. Identify the
      blocking provider and restore a bounded full-plan path before relying on global drift
      output for a rollout gate.
- [ ] **atlas host journal** — the remaining log gap. atlas is the Proxmox/PVE hypervisor,
      **not a k8s node**, so no in-cluster DaemonSet can reach its journal. Ship it with a
      host-level promtail/alloy systemd unit on atlas pushing to Loki over the Nebula mesh
      (needs a mesh-reachable `loki-write` endpoint — `*.svc.cluster.local` doesn't resolve
      off-cluster). This is host config (Ansible), not the k8s GitOps tree. Lower value than
      the guest since the authoritative Xid source is the wyrm2 **guest** journal, not the
      host.

### Operational note: NixOS `node-vendor` label needs a manual apply after a switch

`node-vendor=nixos` is set via `nix/nixos/hosts/*/default.nix` (`k8sWorker.nodeLabels`), but
kubelet only applies `--node-labels` at **first registration**. On an already-registered node
a `nixos-rebuild switch` restarts kubelet **without** adding the new label, so the
`promtail-journal` DaemonSet won't schedule. After switching, verify with
`kubectl get node <n> --show-labels` and, if missing, apply once by hand:
`kubectl label node <n> node-vendor=nixos` (the nix config keeps it correct for future
re-registration). Done for wyrm2 + rugged; iguana needs it whenever it's next online.

## GPU metrics (DCGM) — follow-ups

DCGM exporter is live on wyrm2 (`cluster/k8s/dcgm-exporter/`) and feeding Mimir:
power/temp/clocks, **PCIe replay counters** (`DCGM_FI_DEV_PCIE_REPLAY_COUNTER` — gpu1
already shows replays, the marginal-link canary), ECC, thermal/power violations. Remaining:

- [ ] **`DCGM_FI_DEV_XID_ERRORS` is not exported** on these consumer GeForce RTX 5090s.
      It's in the custom `counters.csv`, but DCGM silently drops field 230 on these cards
      (no value / unsupported). So there is **no XID-as-a-metric** despite the plan's intent.
      Mitigation already in place: the authoritative Xid-79 signal is captured via the
      **journal → Loki** path (`{job="systemd-journal"} |~ "Xid|fallen off the bus"` on
      wyrm2). Optional improvement: try DCGM 4.x's experimental
      `DCGM_EXP_XID_ERRORS_COUNT` collector (separate from field 230) — needs the
      experimental-collectors flag/config on the exporter; verify it actually populates on
      GeForce before relying on it.
- [ ] **Retire `nix/nixos/modules/gpu-monitor.nix`** (the local-CSV poller on wyrm2) once
      the DCGM metrics in Mimir are confirmed to cover the run-up telemetry over a full
      fall-off cycle. Don't remove it before then — it's the only "before" archive.
- [ ] **Per-GPU PCIe AER correctable-error scrape** (followups doc #4). AER counters are
      present inside the wyrm2 guest (`/sys/bus/pci/devices/0000:0{1,2}:00.0/aer_dev_correctable`).
      DCGM's PCIe replay counter already covers the marginal-link canary, so this is
      optional; a small textfile/exporter DaemonSet reading `aer_dev_*` would add the raw
      AER counts.

## agent-box follow-ups

The agent-box VM and its `codex` user are live (see
<agents/agent-box/README.md>). Remaining work:

- [ ] **Enable the attic substituter** on agent-box: the Nix wiring is tombstoned
      in `nix/nixos/hosts/agent-box/default.nix` + `nix/home/hosts/agent-box/common.nix`.
      Flip it on once `attic-jwt-rotation` has minted+committed
      `secrets/hosts/agent-box-attic.yaml` to devel (the path literal would
      otherwise fail flake eval).
- [ ] **`claude` agent user** on agent-box: same multi-user pattern as `codex`
      (the `agentUsers` list + a per-user HM module under `nix/home/hosts/agent-box/`),
      but running Claude Code against Anthropic directly (not via LiteLLM/z.ai).
- [ ] **Auto-provision the Codex CLI auth credential** (`~/.codex/auth.json`):
      today the ChatGPT login is done manually via the device-code flow and is lost
      on every image rebuild + VM recreate. Investigate SOPS-planting it (decrypted
      with the `agent-box-codex-user` key) — caveat: the token likely carries a
      refresh token that rotates on use, so a static plant could go stale; check
      whether the Codex CLI rewrites `auth.json` after refresh.

## Alloy `allow_arbitrary_file_access`: decide the residual components

Native scrapes in `monitoring/alloy/config.alloy` cover apiserver and kubelet,
and clearing the spurious token fixes coredns. Three consumers of
`bearerTokenFile` are still rejected by Alloy and therefore still unscraped:
`monitoring-kube-controller-manager`, `monitoring-kube-scheduler`, and
`volsync-system/volsync`. The last one is emitted by the volsync chart, so
kube-prometheus-stack values cannot reach it.

The alternative is `allow_arbitrary_file_access = true` on
`prometheus.operator.servicemonitors`, which fixes all three at once but grants
file access to every ServiceMonitor in the cluster rather than to named jobs.
Today that is close to free — only `monitoring-operator`, `kubevirt-operator`
and `seaweedfs-operator-manager-role` can write ServiceMonitors, and Alloy
mounts nothing but its own ConfigMap and service-account token — so the flag
would hand a hostile author only a credential they could already obtain.

- [ ] Decide between extending the native scrapes to controller-manager and
      scheduler (needs their endpoints reachable on 10257/10259, unverified
      since the metrics have been absent since 2026-08-07) versus enabling the
      flag for the remainder. Whichever wins, `volsync` needs an answer: a
      `postRenderers` patch on its HelmRelease, an upstream values knob, or the
      flag.
- [ ] Re-evaluate if Alloy ever mounts a Secret. The flag's low cost rests on
      it having nothing worth reading; that assumption is the tripwire.
