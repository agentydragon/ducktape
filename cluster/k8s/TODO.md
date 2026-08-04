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

## Retire the `openclaw-*` namespaces

The OpenClaw gateway and OpenShell stack were deleted on 2026-07-31, but
`openclaw-gateway` and `openclaw-sandbox` survive as credential holders: four
unique secrets pin those namespaces in `metadata`, and since the files set no
`mac_only_encrypted` the document MAC covers that field, so re-homing them is a
`sops` operation and not a text edit. Background: `agents/openclaw/README.md`.

All live teardown debris was cleared on 2026-08-04, in two passes. First the
OpenClaw layer: the stuck `OpenClawInstance/openclaw`, the `StatefulSet` and 21Gi
of PVCs it held, the objects GC took with it (Service, NetworkPolicy, PDB,
Role/RoleBinding, ServiceAccount, ConfigMap, gateway token), and the three
`openclaw.rocks` CRDs Helm left behind. Then the OpenShell layer that surfaced
underneath it — `ConfigMap/openshell-openshell-operator-oidc-jwks`, the
`openshell-gateway-certgen` ServiceAccount/Role/RoleBinding, and the Secrets
`openshell-{client-tls,server-tls,gateway-jwt-keys,openshell-operator-token}`,
none of which carried a Flux label, Helm release annotation or reflector
annotation.

`openclaw-gateway` now holds only the three retained credentials, two reflector
mirrors (`github-token`, `litellm-key-openclaw` — leave both alone, deleting them
only makes the reflector recreate them) and the cluster built-ins. A cluster-wide
sweep found no other `openshell`/`openclaw` remnants: both the
`openshell-sandboxes` and `openshell-system` namespaces are gone, and neither
`openshell.lenshq.io` nor `openclaw.rocks` has any CRD left.

Two gotchas worth keeping:

- Clearing a dangling finalizer needs a **merge patch** (`finalizers: null`) and
  cluster-admin. A server-side apply cannot do it — `metadata.finalizers` is a
  set-type list, and SSA will not remove an entry owned by a different field
  manager, so the apply reports success and changes nothing.
- `agent-lab`'s RBAC still grants `openshell.lenshq.io` **and** `openclaw.rocks`;
  both API groups now have zero CRDs, so both grants are inert. The
  `openclaw.rocks` one only became inert with the CRD deletion above.

What remains here is git-side, in order — the second is blocked on the first:

- [ ] **Move the credentials** (needs the cluster age key). Re-author each under
      `agents/shared-secrets/` with the right namespace and `sops -e -i`:
      `openclaw-{anthropic-api-key,openai-api-key,telegram-bot-token}` from
      `openclaw-gateway`. (`ibkr-flex-query-credentials` was deleted 2026-08-04
      rather than moved.)
      Update the reflector annotations and `docs/bootstrap_dependencies.md` rows
      in the same change. Alternatively **revoke** any you no longer want — at the
      Anthropic and OpenAI consoles and via @BotFather, and in IBKR Account
      Management. Deleting only the Secret leaves a live credential in the wild.
- [ ] **Then delete `agents/openclaw/` entirely** — all four flux kustomizations,
      their root `kustomization.yaml` entries, and the `CLEANUP(added 2026-07-31)`
      tombstones in both `namespace.yaml` files. The gate is
      `grep -rl 'namespace: openclaw-' cluster/k8s/ --include='*.sops.yaml'`
      returning nothing; it lists four files today.

## `openclaw-sandbox` reflector targets in shared secrets

Two SOPS-encrypted Secrets name `openclaw-sandbox` in their emberstack reflector
annotations. That namespace still exists (see above), so this is dormant rather
than stale — it becomes stale the moment the namespace is retired:

- [ ] `agents/shared-secrets/attic-push-token.sops.yaml` — drop `openclaw-sandbox`
      from `reflection-{allowed,auto}-namespaces`
- [ ] `agents/shared-secrets/buildbuddy-api-key.sops.yaml` — same, leaving
      `codex-pod,public-coder-agent`

Not done alongside the deletion because these files set no `mac_only_encrypted`,
so the document MAC covers metadata: a raw text edit of the annotations breaks
decryption with a MAC mismatch. Fixing them needs the age key and a `sops -i`
pass, which an agent without the key cannot do without rotating the value.

Harmless either way: reflecting into a namespace that does not exist is a no-op,
and while it does exist nothing there consumes these. Fold it into the same `sops`
pass that re-homes the credentials rather than making a separate trip.

## Secret layout

- [ ] Restructure the flat agent token files under `secrets/` into clearer
      ownership folders (for example per-agent or per-rotator). Current
      rotators write paths like `secrets/*-k8s-jwt.yaml` and
      `secrets/*-forgejo-tea-token.yaml`; keep `.sops.yaml`, home-manager
      modules, and rotator configs in sync when moving them.

## InvenTree secrets if unsuspending

- [ ] Add SOPS secrets for InvenTree admin and database passwords before
      unsuspending it. The old Vault `kv/inventree/*` values are gone, so
      generate fresh values. Background: <../archive/2026_04_19_vault_migration.md>.

## Mobile Nebula phone followups

- [ ] Wire phone ActivityWatch capture into the sync topology. Decide whether
      Android can reliably run ActivityWatch + Syncthing in the background, or
      whether it needs a phone-specific exporter/importer path. Keep the cluster
      import shape consistent with the desktop path: one phone-owned sync folder,
      provenance-preserving bucket names, and query from the cluster server.
- [ ] Allow SSH from the phone to mesh machines such as `wyrm2`; verify the
      phone has an SSH client/key path, host SSH/firewall policy allows the
      phone's Nebula identity or `10.42.0.50`, and access stays key-only.
- [ ] Consider running an SSH daemon on the phone for emergency access back to
      the device, probably via Termux/OpenSSH, with explicit keys and a clear
      power/background-execution story.

## ActivityWatch storage followups

- [ ] Resolve the SQLite benchmark issue (#2959), then choose where the
      ActivityWatch query server's hot SQLite DB should live. The Syncthing inbox
      stays on `seaweedfs-ovh`, but `activitywatch-data` is still
      `local-path-proxmox` and is therefore node-local failure debt.
- [ ] Move the ActivityWatch query server off the Proxmox-pinned local-path PVC
      once there is a validated storage target or an automated backup/rebuild
      path from the Syncthing-exported source folders.

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
- [ ] **`claude` agent user** on agent-box: same multi-user pattern as `codex`/`zai`
      (the `agentUsers` list + a per-user HM module under `nix/home/hosts/agent-box/`),
      but running Claude Code against Anthropic directly (not via LiteLLM/z.ai).
- [ ] **Auto-provision the Codex CLI auth credential** (`~/.codex/auth.json`):
      today the ChatGPT login is done manually via the device-code flow and is lost
      on every image rebuild + VM recreate. Investigate SOPS-planting it (decrypted
      with the `agent-box-codex-user` key) — caveat: the token likely carries a
      refresh token that rotates on use, so a static plant could go stale; check
      whether the Codex CLI rewrites `auth.json` after refresh.
