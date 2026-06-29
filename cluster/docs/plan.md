# Cluster Roadmap

**Status**: OVH/Kimsufi Talos control-plane and worker nodes, plus NixOS/Proxmox
workers and roaming laptops. Cilium Gateway API, DNS automation, Authentik SSO.
PowerDNS and Authentik run on CloudNativePG `local-path`.

## Dropped Services

- **Capacitor** (2026-03-22): Removed. `ghcr.io/gimlet-io/capacitor-next` requires a
  proprietary license key despite the Apache 2.0 source license — the license check is
  injected in Gimlet's private build pipeline, not in the published source. Weave GitOps
  (the main alternative with dep graph visualization) has had no stable release since
  2023-12-06. Use Headlamp + `flux` CLI instead.

## Suspended Kustomizations

### Intentionally parked

Independent of the Proxmox outage — these stay suspended until explicitly revived,
and would **not** come back just because `atlas`/`wyrm2` returns.

- **ActivityWatch**: `activitywatch` — parked 2026-04-06.
- **ARC**: `arc-namespace`, `arc` — decommissioned 2026-04-11; GitHub runner
  pod/statefulset removed, resources deleted (`arc-secrets` still deployed).
- **BuildBuddy Executor**: `buildbuddy-executor` — scaled to 0; re-enable when needed.
- **Docker CI**: `docker-ci` — parked.
- **Firecrawl**: `firecrawl-{namespace,db,app}` — parked.
- **Google Workspace MCP**: `google-workspace-mcp` — parked 2026-05-13; resources + PVC deleted.
- **Harbor**: `harbor-{namespace,secrets,db,agent-rbac,ci,oidc-config,props,proxy-cache,servicemonitor}` —
  parked 2026-06-11. It was mostly a registry for props, which now use the Forgejo
  registry for backing. (`harbor-db` spec already moved to `local-path-ovh` /
  `region=hil` should it ever be revived.)
- **HomeAssistant Proxy**: `homeassistant-proxy` — parked 2026-06-01 (15 Leroy household left
  2026-06-27); workload objects removed from cluster, manifests kept suspended for possible
  revival at a new place.
- **InvenTree**: `inventree-{namespace,secrets,token-provisioner}`,
  `authentik-blueprint-inventree-secret` — nice-to-have, parked under capacity pressure.
- **kagent**: `kagent-{crds,db,secrets}` — parked 2026-05-08; too fragile (sessions die on
  large MCP outputs, z.ai error 1261). Resources deleted. See <../k8s/agents/kagent/TODO.md>.
- **Matrix**: `matrix`, `matrix-{db,secrets,namespace}` — parked.
- **OpenClaw**: `openclaw-{gateway,operator,sandbox}` (+ their `-namespace`/`-secrets`,
  `gateway-agent-rbac`) — experimental, parked. **Cluster objects deleted 2026-06-19**:
  the `openclaw-{operator,mitmproxy}` namespaces (operator Deployment/HelmRelease/pod) and
  the `openclaw-operator-manager-{role,binding}` ClusterRole/ClusterRoleBinding. The
  operator was already broken before teardown — its HelmChart had no artifact and the
  operator image `ghcr.io/openclaw-rocks/openclaw-operator:v0.11.1` was in
  `ImagePullBackOff` — so it sat firing `FluxHelmReleaseNotReady` /
  `KubeDeploymentReplicasMismatch`. **To revive: fix the operator HelmChart/image pull
  first, then un-suspend the kustomizations** (the git manifests are intact). The
  `authentik/openclaw` HTTPRoute is left in place (owned by the active authentik
  proxy-routes kustomization, backend is the authentik outpost) and will 502 until revived.
- **OpenHands**: `openhands`, `openhands-{namespace,secrets,sandboxes}` — experimental, not
  currently used.
- **Tandoor**: `tandoor`, `tandoor-{db,namespace}` — using Grocy instead.
- **claude-sandbox-firecracker** — parked to free resources.
- **listing-monitor-smoke** — parked. (thrive-scraper was un-parked by moving it to
  git-based storage in Forgejo — no PVC, no wyrm2 pinning; see gaffer-private
  `x/thrive_scrape/DESIGN.md`.)

### Down while Proxmox (`atlas`/`wyrm2`) is offline

Suspended only because the home Proxmox host is offline; should auto-recover when it
returns (not independently parked):

- **proxmox-proxy** — the Proxmox API proxy; needs `atlas`.
- **sdr**, **scanner** — waiting on `atlas` (sdr also needs the radio re-set-up post-relocation).
- **cpap-sync** (CronJob `suspend`) — waiting on `wyrm2`. Now pushes to the `cpap-data`
  Forgejo repo (no PVC), so generalizing it to roaming devices near the CPAP is feasible —
  any machine with the write creds can run the sync.
- Not suspended in git, but non-functional while `wyrm2` (GPU) is down: **ollama**,
  **nvidia-device-plugin**.

## Next Actions

- [ ] **etcd lease-PUT latency / control-plane HDD I/O contention.** etcd runs on
      rotational HDDs on the KS-5 control planes (no SSD there; the NVMe is on the
      KS-GAME workers). **Recurred 2026-06-28 as a full outage** (two CPs NotReady,
      Forgejo 500s); a `vm-images-publisher` build wrote ~15 GB to a CP disk and
      starved etcd. Applied so far: defrag + flux-controller pins (2026-06-19); soft
      anti-affinity on all ~22 hil-ovh stateful workloads + hard anti-affinity on the
      `vm-images-publisher` CronJob (2026-06-28, PR #2614). Remaining: **actively
      migrate the running stateful pods off the CP nodes** (anti-affinity is
      forward-only — node-by-node, health-gated; CNPG/Loki/Mimir use node-pinned
      `local-path-ovh`, so moving an instance is a re-clone; the single `seaweedfs-filer`
      is the one workload that can't roll without a brief SeaweedFS-wide blip); pin the
      tofu-controller runners (blocked on centralizing the ~22 copy-pasted
      `runnerPodTemplate`s) and the cross-repo augur ingest job; then the structural
      etcd-on-NVMe move. Full RCA + remediation tracking:
      <lessons_learned/2026_06_19_etcd_hdd_io_contention.md>.
- [ ] **haku-ci Docker Hub pull-through cache** (replace the external-CDN egress
      allows). The haku-ci runner's rootless dind pulls base images from Docker
      Hub, which 307-redirects config/layer blob GETs to `production.cloudfront.docker.com`
      (AWS CloudFront). The egress policy now allowlists that host explicitly
      (root cause was a `cloudflare` vs `cloudfront` hostname mix-up — only the
      Cloudflare front was allowed). It works but couples the runner to Docker's
      CDN hostnames. Replace with an in-cluster `registry:2` configured as a Docker
      Hub pull-through cache: transparent via the dind `--registry-mirror` (Harbor's
      proxy-cache can't act as a root mirror, and Harbor is currently down anyway),
      then drop the docker.com CDN allows. Bonus: caching + Docker Hub rate-limit
      relief.
- [ ] **Investigate whether to re-enable VPA/Goldilocks recommendations.**
      Forgejo's namespace is Goldilocks-enabled and has a generated
      `goldilocks-forgejo` VPA, but the VPA control-plane deployments in
      `kube-system` (`vpa-recommender`, `vpa-updater`,
      `vpa-admission-controller`) are currently scaled to 0, so no
      recommendations or automatic updates are happening. Find when/why VPA
      was disabled, decide whether Goldilocks should be active again, and
      document the intended mode if it should stay off.
- [ ] **Wire gecko's bootstrap image to autoprovision (stable `latest` key).**
      gecko's `gecko-root` DataVolume hardcodes a specific
      `bootstrap/<sha>.qcow2` URL, so when that object's SeaweedFS chunks were
      lost on 2026-06-02 it needed a manual re-pin (#1980, to the readable
      `da37375538…` = current `devel.latest.txt` target). The
      `vm-images-publisher` already writes a `bootstrap/<ref>.latest.txt`
      pointer, but a DataVolume can't dereference a pointer file. To make this
      self-healing: have `publish.sh` also `aws s3 cp` to a stable key
      (`bootstrap/<ref>.latest.qcow2`, overwritten each publish) and point
      gecko's DataVolume at that — then refreshing the image is just a publish + DV recreate, no manifest edit. Caveat: the DataVolume `source` is a
      one-shot seed and is gecko's root disk, so recreating it wipes the disk
      (re-seed, not in-place upgrade). The dead `bootstrap/3b9e37c50911…qcow2`
      phantom (+ sidecar) has been deleted, but several orphan
      `bootstrap/<sha>.qcow2.sha256` sidecars with no surviving qcow2 still
      linger in the `vm-images` bucket and could be swept too. See
      <lessons_learned/2026_06_02_seaweedfs_volume_loss_ovh_rename.md>.
- [ ] **Fix SeaweedFS rack labels.** All three OVH volume servers currently
      advertise `rack=hil-ovh-h109b04`, so the `defaultReplication: "001"`
      policy effectively means "any other DataNode" rather than "another
      rack". Either give each volume server a distinct rack id (or just
      `rack=$nodeName`) or document that we have single-node tolerance only.
      Rolling-PVC runbook is now at
      <runbooks/rolling_seaweedfs_volume_pvc.md>; the rack-labels caveat is
      called out there too.
- [ ] **Add ReplicationSource for gitea-shared-storage** (and any other
      SeaweedFS-backed PVC holding non-regeneratable state). Currently
      only `grocy-{sf,vallejo}` and `tana-mcp` have volsync backups; the
      Forgejo loss above was survivable only because nothing had been
      pushed yet, not because we had a backup. Destination choice is
      non-trivial: `seaweedfs-ovh`→`seaweedfs-ovh` doesn't protect against
      the failure mode we just hit (defeats the grocy/tana pattern when
      the primary is already on SeaweedFS), and `local-path-proxmox` is
      currently unavailable because Proxmox is down. Likely needs real
      off-cluster object storage (B2 / R2 / OVH Object Storage) — overlaps
      with the observability-backend decision below.
- [ ] **Convert PVC backups to snapshot/retention, not Direct rsync.**
      Current `grocy-{sf,vallejo}` and `tana-mcp` volsync configs use
      `copyMethod: Direct` rsync into a single destination PVC. When the
      source PVC ends up empty for any reason — fresh reprovision after a
      node rename, a wipe, accidental delete-and-recreate — the next
      scheduled sync **overwrites the only good backup with the empty
      source**, with no prior version retained. Almost happened to
      grocy-vallejo on 2026-06-03 during the
      `talos-kimsufi-worker-1`→`ovh-ns103711` rename: the rename
      half-attached `/var/mnt/seaweedfs-data`, the `grocy-config-ovh` PVC
      re-provisioned empty, and only the orphan pre-rename local-path dir
      under
      `/var/mnt/seaweedfs-data/local-path/pvc-0f9e70aa-…_grocy-vallejo_grocy-config-ovh/`
      saved us — both the 06:29 UTC scheduled backup and the home-dir
      precautionary copy had already captured the empty state. Two
      candidate fixes: (1) `copyMethod: Snapshot` with a
      `VolumeSnapshotClass` and retain N snapshots — volsync's native
      pattern, but needs the CSI driver to support snapshots, which
      `seaweedfs-csi` may not; (2) **Restic** to off-cluster object
      storage (B2/R2/OVH) with a proper retention policy (e.g. keep 7
      daily, 4 weekly, 6 monthly) — Restic dedupes so retention is cheap,
      and a source going empty only adds a new snapshot, not destroys
      old ones. Overlaps with the off-cluster object-storage decision in
      the items above. See
      <../debug/2026-06-03-ns103711-seaweedfs-data-volume-mount-missing.md>
      for the close call.
- [ ] **Decide whether observability storage stays on SeaweedFS**. Loki,
      Mimir, and Tempo all lost data in the rename. If those backends are
      supposed to survive node-rotation incidents, they should live on
      off-cluster object storage (B2 / R2 / OVH Object Storage / etc.)
      rather than home-hardware SeaweedFS.
- [ ] **Bring SeaweedFS up properly on every OVH node, or stop claiming we
      do.** Convention is that every OVH server (workers and CPs) runs a
      SeaweedFS volume server. As of 2026-06-03 only 3 of 5 do: volume-0 on
      ovh-ns104952, volume-1 on ovh-ns103656, volume-2 on ovh-ns102453.
      Gaps: - **ovh-ns103711** (KS-5 CP, ex-kimsufi-worker-1): no volume server
      AND its `/var/mnt/seaweedfs-data` mount is read-only. Talos
      `UserVolumeConfig` for `/dev/sdb`+xfs IS in
      `cluster/terraform/main/ovh-nodes.tf:273-291`, but the volume
      either never came up post-rename or `/dev/sdb` doesn't exist on
      this physical server. Currently cordoned (2026-06-03) so the
      broken local-path-ovh entry stops biting study-casino-db's
      rebuild. Lying-by-omission: `cluster/k8s/local-path-provisioner/helmrelease.yaml`
      lists it in `nodePathMap` as if the disk were mounted. - **ovh-ns104963** (KS-GAME worker): disk is fine
      (`/var/mnt/seaweedfs-data` works — study-casino-db-5 just
      provisioned there), but no SeaweedFS volume server runs on it.

      Either fix it (diagnose 103711's disk via `talosctl --nodes 10.42.0.14
      get uservolumeconfig` / `talosctl ls /var/mnt`, fix the Talos config
      if needed, then bump Seaweed CR `spec.volume.replicas: 3 → 5` and
      verify replication converges across all 5 nodes; uncordon
      ovh-ns103711) or stop claiming the broken state is fine (remove
      103711 from `nodePathMap` and the topology entirely until it's
      actually brought up). Pick one. The current half-state is the
      worst of both — directly caused the study-casino-db migration to
      stall on a broken-disk node selected by the scheduler. Also fixes
      `defaultReplication: 001` durability headroom (4-5 volume servers
      → tolerates 2-node loss instead of just 1).

- [ ] **Diagnose tana-mcp crash-loop** — used to work before the renames.
      `tana-desktop` container restarts every ~3 min (43+ restarts as of
      2026-06-02 evening) with exit code 137 on a Chromium renderer
      subprocess. The `Permission denied (OOM score adjust)` and
      `Failed to connect to dbus` errors visible in current logs ALSO appear
      in the previous-pod logs from before today (i.e. they are normal
      noise, not the cause). The actual regression vs pre-rename must be
      elsewhere. Possibilities to investigate: (1) the restored profile from
      volsync backup is corrupted/incomplete — verify against backup file
      counts, consider a fresh init; (2) startup-ordering bug — tana-desktop
      may now be racing against `mcp-valkey-ovh` readiness; (3) something
      about the new node's resource set (CPU model, hugepages, sysctls)
      differs from the old one. Diff `tana-desktop` logs from a previously
      working pod against current.
- [ ] **Eliminate per-node hostname references** in repo files
      (`cluster/k8s/local-path-provisioner/helmrelease.yaml`'s `nodePathMap`,
      `nebula-mesh.json` keys, etc.). Every node rename today requires editing
      a fixed list of files; ideally local-path-provisioner could match OVH
      nodes via a node-label selector (e.g. `topology.kubernetes.io/zone:
hil-ovh`) and apply the same `nodePathMap` entry to any matching node.
      Investigate: does Rancher local-path-provisioner support label-based
      node selection in `nodePathMap`? If not, consider a small mutating
      controller or a different provisioner. Same idea could apply to the
      Nebula mesh roster (derive entries from cluster Node labels rather than
      a static JSON).
- [ ] **Rename Terraform local-map keys** in `cluster/terraform/main/ovh-nodes.tf`
      and `cluster/terraform/main/nebula.tf` to match the renamed hostnames.
      Keys currently leak role/index (`kimsufi_cp0`, `kimsufi_worker0/1`,
      `ks_game_worker0/1`) even though hostnames are now `ovh-ns102453`,
      `ovh-ns103656`, `ovh-ns103711`, `ovh-ns104952`, `ovh-ns104963`. Naive edit
      forces destroy+recreate of every `for_each`-keyed resource — including
      `null_resource.install_talos_kimsufi` (which would `dd` the Talos image).
      Use `tofu state mv` (~8 ops per node) to rekey without recreation; see
      `plans/rename_ovh_nodes_role_neutral.md` § "Follow-up: rekey Terraform
      local-map keys" for the procedure. No downtime if done correctly; defer
      until all five hostname renames are stable.
- [ ] **Test CNPG full self-recovery from node failure** in a controlled setup.
      During the 2026-06-02 OVH node renames (see
      `debug/2026-06-02-tofu-apply-hangs-from-rugged-mtu.md`) we deleted
      hostname-pinned replica PVCs after node drains. Mixed observations:
      `airlock-db` self-recovered to 2/2 in ~70min; `atuin-db`, `plaid-mcp-db`,
      `props-db`, `forgejo-db` got stuck for 2+ hours in
      `instances=2 ready=1, STATUS=Waiting for the instances to become active`
      with replacement pods Pending and PVC never recreated, until manually
      `kubectl delete pod`'d (which prompted CNPG to rebuild fresh in ~5min).
      Hypothesis: operator was startup-probe-thrashing during pilot 4 cleanup
      (6 restarts in 54m); a partial reconcile created the Pod manifest but
      never got to creating the PVC before the next restart. Worth reproducing
      without API server pressure: kill one CNPG replica's PVC + pod cleanly,
      observe self-heal vs needing nudging.
- [ ] **Replace Kimsufi worker_0** with the second new KS-5 from order #7958582
      once it's delivered. Sequence: cordon + drain `talos-kimsufi-worker-0`,
      `talosctl shutdown` the cancelled `ns103656` server, `kubectl delete node`,
      `tofu state rm` the 9 worker_0 entries, set `var.kimsufi_service_name` to
      the new server name, targeted `tofu apply`. Then update
      `nebula-mesh.json:static_host_map` with the new public IP for `10.42.0.13`.
      See <kimsufi_provisioning.md> §5 (path B already executed for worker_1 from
      the same order — `ns103711.ip-147-135-39.us` joined as
      `talos-kimsufi-worker-1` on 2026-05-15).
- [ ] **Re-key admin-only SOPS files to include user keys** — admin age key currently
      lives only on wyrm2, which is offline in a SF storage unit. `.sops.yaml` rules say
      `secrets/nebula/ca.sops.key` and `k8s/tofu-state/db/credentials.sops.yaml` (among
      others) should be encrypted to admin + 4 user keys, but the files in git still have
      only admin. Discovered 2026-05-13 trying to provision the Kimsufi node from rugged.
      Blocks: `tofu apply` of any resource needing the Nebula CA (e.g. `null_resource.nebula_node_cert`
      for the Kimsufi worker). Also blocks `tofu init` (PG backend creds in
      `tofu-state/db/credentials.sops.yaml`). - Partial workaround for PG backend: fetch creds from k8s instead of SOPS in
      `cluster/.envrc` — `kubectl -n tofu-state get secret tofu-state-db-credentials`
      works with current agent RBAC base/user kubeconfig. - Real fix when wyrm2 is back: `sops updatekeys` on every file where `.sops.yaml`
      lists more recipients than the file actually has. Audit with
      `for f in $(git ls-files '*.sops.*'); do jq -r '.sops.age[]?.recipient' $f; done`
      and compare to expected recipients per rule.
- [ ] **Restore SDR kustomization** — unsuspend `cluster/k8s/sdr/flux-kustomization.yaml`
      (remove `suspend: true`) once the radio hardware is set up at the new place.
- [ ] **Resume CPAP sync CronJob** — unsuspend `cluster/k8s/cpap-sync/cronjob.yaml`
      (remove `suspend: true`) once wyrm2 is back online after relocation. First run
      re-seeds the fresh `cpap-data` Forgejo repo with the card's full history (~17+ min);
      verify the commit lands. Also check the orphaned PVC-era `cpap-data` PV finally
      deletes once wyrm2's CSI plugin is back.
- [ ] **Set up offsite tofu-state backup** — the Proxmox-pinned `pg_dump` CronJob
      (`cluster/k8s/tofu-state/backup/`) was deleted 2026-06-02: it couldn't run with
      wyrm2/Proxmox down and only wrote to a `local-path-proxmox` PVC (same failure
      domain as the DB, not actually offsite). Set up a real offsite backup of the
      `tfstate` DB — e.g. CNPG `barmanObjectStore` → SeaweedFS S3, or an
      always-on OVH-node CronJob streaming dumps to S3.
- [ ] **Evaluate lighter registry to replace Harbor** — Harbor is only used for (a) pull-through
      proxy cache (Docker Hub, GHCR, GCR, Quay, k8s.io) configured as Talos containerd mirrors,
      and (b) props agent image storage. Candidates: [Zot](https://zotregistry.dev/) (single binary,
      multi-upstream proxy + private images), or separate `registry:2` per upstream + GHCR for props.
      Would drop the Harbor Helm chart, CNPG cluster, 32Gi HDD PVC, ~700Mi RAM, and all
      Harbor-specific TF modules.

- [ ] Verify dmeventd thin pool monitoring after wyrm2 reboot: NixOS config changed
      `pkgs.lvm2` → `pkgs.lvm2_dmeventd` so `lvchange --monitor y` actually registers
      with dmeventd. After next wyrm2 reboot, verify with
      `lvs -o lv_name,seg_monitor -S "lv_attr=~^t"` — Monitor column should show
      `monitored`. See `nix/nixos/hosts/wyrm2/default.nix` openebs setup services.
- [ ] Fast token rotation on cluster reprovision: `claude-token-rotation` CronJob runs
      biweekly (1st/15th), so after a cluster rebuild the SOPS-encrypted token in
      `secrets/claude-web-k8s-jwt.yaml` is stale until the next scheduled run. Need a
      mechanism to rotate immediately on reprovision — e.g., trigger the job from
      bootstrap, or add a post-bootstrap hook. Also consider whether the bootstrap script
      itself should decrypt the new token and inject it into the local environment so
      Claude Code sessions work immediately without waiting for the git push + re-source
      cycle.
- [ ] Require every workload to declare Stakater Reloader explicitly as enabled or
      intentionally disabled. No implicit default. Enforce via review/docs and
      add missing `reloader.stakater.com/auto: "true"` or an explicit opt-out
      comment/setting on existing Deployments, StatefulSets, and Helm releases.
- [ ] Restore docker-ci Gateway routing: docker-ci needs TLS passthrough on port 2376.
      Previously used a dedicated `docker-ci-tls` Gateway listener + TLSRoute, but Cilium
      bug [#42159](https://github.com/cilium/cilium/issues/42159) caused that listener's
      `allowedRoutes.kinds: [TLSRoute]` to bleed into all listeners, blocking all HTTPRoutes.
      Options: (a) move docker-ci to HTTPS on port 443 with a subdomain, (b) separate Gateway
      resource for docker-ci only, (c) wait for Cilium fix and re-add the listener with
      `sectionName` on all HTTPRoute parentRefs. Also consider deriving Gateway IP pools
      from OVH node inventory rather than hardcoding addresses.
- [ ] Cilium Gateway API `Programmed=False` (#42786): hostNetwork mode leaves the Gateway
      status as `Programmed=False` / `AddressNotAssigned`, even when Cilium has generated
      the `CiliumEnvoyConfig`, Envoy listeners are present on the selected nodes, and
      routes work. Current decision: keep the hostNetwork Gateway plus Route 53 records
      pointing at the public OVH Kubernetes node IPs. Do not alert solely on
      `Programmed=False` for `gateway-system/cluster-gateway`; use blackbox probes
      against the public node IPs and Cilium/Envoy programming signals instead.

      More normal Cilium exposure models, if we decide to leave hostNetwork mode:

      1. Provider-managed load balancer: public OVH LB IP -> Kubernetes
         `LoadBalancer`/`NodePort` Service -> Cilium service handling -> Envoy ->
         backends. This is the ordinary "intermediate entity" model: the provider owns
         the routed public IP and health/failover behavior.
      2. Provider-routed or floating VIPs: OVH Additional IP / routed address block ->
         Cilium `CiliumLoadBalancerIPPool` assigns Service IPs -> Cilium BGP or L2
         announcement advertises them. LB-IPAM only allocates IPs; it does not make
         arbitrary public addresses reachable unless the provider network routes them
         to us.
      3. External LB to NodePort: a provider or self-hosted load balancer targets node
         ports, while Cilium still handles the in-cluster service path.

      If we move to one of these, disable `gatewayAPI.hostNetwork.enabled` and let the
      generated Gateway Service be the externally exposed object. Setting
      `Gateway.spec.addresses` to static OVH node IPs is not a real replacement for a
      routed VIP/LB; it may help status only if Cilium supports that shape, but it
      would not add failover or change internet routing.

- [ ] Autopopulate `tf/gitops/dns-records` IP lists from cluster state instead of a
      hand-edited literal. After every `talos-* → ovh-ns*` rename the comments rot
      (none of those rename commits touched the DNS TF) and IPs of nodes whose Cilium
      L2 announce is stale stay in the `*.allegedly.works` round-robin until a human
      notices — most recently the augur oauth2-proxy crash-looped on OIDC discovery
      against `auth.allegedly.works` because DNS resolved to a dead IP ~2/5 of the
      time (PR fixing the immediate bleeding: ducktape#1820).

      Survey of where the public IPs actually live today:

      - **OVH cluster TF**: `cluster/terraform/main/ovh-nodes.tf:335-337` already rolls
        `data.ovh_dedicated_server.kimsufi[*].ip` into `local.kimsufi_public_ips`.
        This is the canonical, live source of truth and would expose as one output.
      - **Kubernetes Node objects**: NOT currently usable. Checked
        `kubectl get node ovh-ns102453 -o json` — `status.addresses` has only
        `InternalIP=10.42.0.15` (Nebula) and hostname; no `ExternalIP`, no public-IP
        annotation, `spec.providerID` empty. The talos-CCM is *installed* (Flux
        `k8s/talos-cloud-controller-manager/helmrelease.yaml`, configured with
        `publicIPDiscovery: true` for `topology.kubernetes.io/region=hil`) but
        switched off: its log says `is kubelet has args: --cloud-provider=external on
        the node?` — the Talos kubelet isn't started with that flag, so it never
        applies the `node.cloudprovider.kubernetes.io/uninitialized` taint, so the
        CCM's `cloud-node` controller short-circuits without populating addresses.
      - **`CiliumLoadBalancerIPPool`**: not applicable to the current hostNetwork
        Gateway. It would become relevant only if we switch to Service exposure
        backed by provider-routed/floating VIPs plus BGP/L2 advertisement.

      Two paths, mostly orthogonal:

      1. **Fix the data gap at the right layer**: add
         `machine.kubelet.extraArgs.cloud-provider: external` to the Talos machine
         config for every Kimsufi node. Existing CCM then populates `ExternalIP` +
         `providerID`. The DNS TF (and `kubectl get nodes -o wide`, and anything
         else that asks the cluster for node addresses) just works. Needs a
         per-node config patch + reboot; check kube-vip / Cilium
         tolerate the temporary uninitialized taint at startup.
      2. **Wire DNS TF to cluster TF state**: add a
         `terraform_remote_state` data source for the cluster TF root and pull
         `local.kimsufi_public_ips`. Lower blast radius (TF-only), no node restart,
         but only fixes DNS — leaves Node objects still missing ExternalIP for
         everyone else.

      (1) is the right architectural answer; (2) is the right next-PR answer.

- [ ] Decouple wyrm2 from tofu: `module.wyrm2` in the same TF root as the cluster means
      any `tofu apply` risks rebooting wyrm2 (the machine running tofu). The `--exclude`
      flag is a workaround but error-prone. Options: separate TF root for wyrm2, or manage
      wyrm2 VM config purely via NixOS/Proxmox API (no terraform). See postmortem
      `cluster/docs/lessons_learned/2026_04_01_cluster_nuke_postmortem.md`.
- [ ] Move Flux to Talos inline/extra manifest (CCM already done via `talos-ccm.tf`).
      Cilium stays as `null_resource.cilium_bootstrap` (helm CLI) because it is large
      and easier to manage after the k8s API is reachable. Gateway API CRDs could move
      to `extraManifests` (URL fetch) but currently also use `null_resource` for
      consistency with Cilium.
- [ ] Consolidate tofu plan prerequisites — credentials are now SOPS-managed
      (`shared/cluster-tokens.yaml`, `credentials.sops.yaml`) and auto-decrypted by
      `cluster/.envrc`. Remaining gaps: - `kubeconfig`: written to `terraform/main/kubeconfig` only after
      `tofu apply`; must be manually copied before the kubernetes provider
      can plan - `talosconfig.yml`: similarly written by `tofu apply`; empty stub in
      checkout, real copy lives in `terraform/main/` on wyrm2 after bootstrap - `PG_CONN_STR`: password from SOPS, but ClusterIP lookup needs `kubectl`
      (falls back to `localhost:15432` port-forward)

- [ ] Authentik blueprint secret rotation: blueprints use file content hash to decide
      whether to re-apply, and `!Env` tags resolve _after_ the hash check, so rotating a
      secret consumed via `!Env` updates the K8s secret + pod env vars but Authentik's DB
      never picks it up. Mostly obsolete now that SSO client secrets live in
      `tf/gitops/sso-providers/` (TF writes the provider directly), but any remaining
      `!Env`-tagged blueprint values would hit this. Force re-application via Stakater
      Reloader + a post-start hook clearing `last_applied_hash`, or a CronJob calling the
      Authentik API.
- [ ] Authentik cache/channels offload: design a Redis-based replacement for the current
      Postgres-backed cache/channels path (`django_postgres_cache`,
      `django_channels_postgres`) without regressing single-node-failure resilience on the
      OVH side. Decide whether this needs HA Redis/Sentinel, an acceptable degraded mode,
      or a different architecture before wiring Authentik to use it. Re-measure steady-state
      Postgres connections afterward before deciding whether any DB limit or
      worker-concurrency changes are still needed.
- [ ] Investigate why `proxmox-csi-retain` publishes zero `CSIStorageCapacity` objects —
      the scheduler sees 0 available space and refuses to schedule `WaitForFirstConsumer`
      PVCs, creating a deadlock. Likely a Proxmox API connectivity issue in
      `proxmox-proxy`. Check `csi-proxmox` controller logs and `proxmox-proxy` logs.
- [ ] Consider OpenEBS LVM on Talos nodes; Talos has `machine.disks` or `machine.volumes`.
      Would allow firecracker fast-clone on OVH workers.
- [ ] Trial SeaweedFS with both the upstream operator and CSI driver. Deploy the
      Seaweed cluster via `seaweedfs-operator`, then install the CSI driver as a
      separate Flux `HelmRelease` pointed at the operator-created filer service.
      Keep the StorageClass non-default, test with disposable PVCs first, and
      evaluate S3/object usage separately from FUSE/PVC usage. Do not use it for
      CNPG or core infra unless the trial proves recovery, upgrades, and node
      restarts are boring.
- [ ] Enable systemd watchdog for kubelet on NixOS workers (`WatchdogSec=` in kubelet
      service unit) — restarts kubelet if it deadlocks
- [ ] NVIDIA GPU monitoring: add DCGM exporter ServiceMonitor + Grafana dashboard (gnetId 12239)
- [ ] etcd: add dedicated ServiceMonitor for full etcd metrics (current scrape is partial via apiserver, now via Alloy)
- [ ] **Roaming node DaemonSet problem** (high priority): Offline roaming nodes
      (iguana/rugged) cause DaemonSet pods to stay Pending, which makes Helm
      install/upgrade timeout. Current workaround is `disableWait: true` in
      monitoring-stack — this is unsatisfying because it suppresses all readiness
      checking, not just for roaming nodes. Affects every HelmRelease with a
      DaemonSet. Need a proper solution:
  - Option A: Taint roaming nodes + add tolerations to DaemonSets that should
    run there (node-exporter, Cilium). DaemonSets without toleration skip roaming.
  - Option B: `nodeAffinity` on DaemonSets to exclude `roaming` region entirely.
  - Option C: Custom Flux health check that ignores Pending pods on NotReady nodes.
  - `rugged` already has `NoSchedule` taint; `iguana` does not — add it.
- [ ] Enable roaming-tolerant workloads on rugged (`grocy`, `scanner`, `activitywatch`,
      `proxmox-proxy`, `props`/`props-registry`)
- [ ] OpenClaw: obfuscation detection forces approval despite `security: full`
      (upstream `0e28e50b4`, PR #24287)
- [ ] OpenClaw: fix Ollama model discovery timeout on startup (Nebula not ready)
- [ ] OpenClaw: eliminate custom image (`ghcr.io/agentydragon/openclaw-matrix`).
      Three steps: (1) publish the airlock plugin as an npm package and install via
      `spec.plugins`, (2) move the `airlock-auth-proxy` sidecar to a standalone
      Deployment+Service in `openclaw-gateway` namespace (CNP-gated to openclaw pod
      only — preserves OAuth2 identity, no security loss), (3) point the plugin config
      at the new service URL instead of `127.0.0.1:8767`. This decouples the proxy
      lifecycle from the StatefulSet (no OpenClaw restart for proxy image updates) and
      lets the instance use the stock upstream image.
- [ ] OpenClaw: StatefulSet RollingUpdate won't replace crash-looping pods. The K8s
      StatefulSet controller counts a crash-looping pod as unavailable, and with default
      `maxUnavailable: 1` the budget is already spent, so it refuses to delete the pod
      for update. This means image updates (e.g. from Flux image automation) are never
      rolled out while the pod is unhealthy. The openclaw-operator (v0.11.1, checked
      through v0.26.2) doesn't set `MaxUnavailable` and exposes no CRD knob for it.
      Workaround: `kubectl delete pod openclaw-0 -n openclaw-gateway`.
      Fix: upstream the operator to set `maxUnavailable: "100%"` for single-replica
      StatefulSets, or add a `spec.updateStrategy` passthrough field.
- [ ] OpenClaw: eliminate one-time token entry
- [ ] Cilium Gateway `Programmed: False` (upstream bug `cilium/cilium#42786`):
      hostNetwork gateways currently report `AddressNotAssigned` even though traffic
      works through the hostNetwork Envoy listeners. Current workaround is Route 53
      wildcard/apex records pointing directly at public OVH Kubernetes node IPs.
      Revisit only if Cilium fixes hostNetwork Gateway status or if we introduce a
      normal external exposure layer: provider-managed load balancer, routed/floating
      OVH VIPs with Cilium LB-IPAM plus BGP/L2 advertisement, or an external LB
      targeting NodePorts.
- [ ] File upstream: powerdns-operator "stuck Failed" bug — once a ClusterRRset
      reaches Failed, it never retries unless spec changes. Should retry with backoff.
      See <lessons_learned/2026_04_07_powerdns_operator_stuck_failed_rrsets.md>.
- [ ] Tandoor: verify deployment works end-to-end (DB migration, Authentik
      proxy auth, recipe import)
- [ ] Fix Goldilocks VPA over-requesting memory on HIL pods. VPA `updateMode: Auto`
      mutates pod requests on creation but old pods retain stale high values until
      restarted. This caused grocy-sf to fail scheduling (97% memory requested on HIL
      workers despite 65-78% actual usage). **Root cause**: Goldilocks auto-creates VPAs
      with no resource policy, and VPA recommendations accumulate historical peaks.
      **Proposed solution**: (1) Add `goldilocks.fairwinds.com/vpa-resource-policy`
      annotations to all namespaces with memory min/max bounds (enforce via Kyverno).
      (2) Switch VPA `updateMode` from `Auto` to `Initial` for non-critical workloads
      so stale requests don't block scheduling — let pod restarts pick up new values
      naturally. (3) Consider running the Kubernetes Descheduler (`k8s-sigs/descheduler`)
      with `RemoveDuplicates` and `LowNodeUtilization` strategies to spread pods across
      HIL workers and evict those with grossly inflated requests. Descheduler is the
      right tool for _rebalancing_ after VPA shrinks requests, but the root fix is
      bounding VPA recommendations via resource policies so they never balloon in the
      first place.
- Loki + SeaweedFS S3 (MinIO retired 2026-05; Loki/Tempo/Mimir all point at the
  SeaweedFS S3 gateway on OVH):
  - [ ] **Host log capture**: Add durable Talos node-level log collection for
        kernel ring buffer / dmesg-equivalent output plus key host services
        (`machined`, kubelet, containerd, KubePrism). The 2026-05-31
        `talos-kimsufi-worker-1` wedge left only pod logs in Loki; once Talos
        API was wedged, `talosctl logs` and `talosctl dmesg` were unavailable
        and the kernel evidence did not survive reboot. Prefer a privileged
        node-local collector that writes to Loki or another durable store with
        labels for `node`, `boot_id`, `talos_service`, and `source`, independent
        of `talosctl` availability during an incident.
  - [ ] **Off-site backup of log history**: replicate the OVH SeaweedFS
        buckets to a second location (Cloudflare R2, AWS S3, or a second
        SeaweedFS instance on Proxmox) for disaster recovery. Overlaps with
        the "Decide whether observability storage stays on SeaweedFS"
        followup above.
  - [ ] **Split Loki write path by region**: Proxmox node logs write to a
        Proxmox-local object store, OVH node logs write to OVH SeaweedFS.
        Avoids cross-site traffic for log ingestion. Grafana queries both.
- [ ] Re-enable MFA (TOTP/WebAuthn) once device enrollment is set up
- [ ] Wire `cluster/scripts/check_authentik_login.py` into bootstrap/CI
- [ ] Gatus: Harbor robot token for authenticated `/v2/` probe
- [ ] Proxy outpost HA: shared session storage (1 replica limit, sessions in `/dev/shm`)
- [ ] Airlock OAuth: upgrade Google scopes (needs approval flow) — `calendar`, `gmail.send`,
      `gmail.compose`, `drive`, `spreadsheets`
- [ ] Airlock OAuth: add readonly scopes — Photos, Tasks, Slides, Forms
- [ ] Airlock OAuth: reflect only access tokens (not refresh tokens) to consumer namespaces
- [ ] Proxmox OIDC auth via Authentik (native OIDC realm in PVE)
- [ ] Proxmox SPICE proxy routing via cluster ingress
- [ ] Proxmox: switch to direct HTTPRoute after OIDC auth (remove proxy outpost wrapper)
- [ ] File Browser (scanner): switch to native OAuth2/OIDC
- [ ] Ollama: per-user auth (Authentik JWTs or LiteLLM proxy)
- [ ] LiteLLM: `ollama/` provider drops `tool_calls` — use `openai-chat` variants for now
- [ ] Harbor terraform: switch to robot accounts
- [ ] Harbor CI robot: scope per-namespace pull secrets to read-only per project
- [ ] Harbor proxy cache: add GHCR credentials for private repos (403 on `openclaw/openclaw`)
- [ ] Verify ntfy.sh notifications
- [ ] ActivityWatch: Gatus health check (`activitywatch-readonly:5600/api/0/info`)
- [ ] ActivityWatch: public access via Authentik proxy outpost

## Production Cutover (`agentydragon.com`)

- [ ] Website hosted in cluster, verify accessible
- [ ] Update `agentydragon.com` DNS to point to cluster
- [ ] Decommission ansible-managed VPS:
  - [ ] Clean up remaining: stale nginx sites, stale home dirs, `/tmp` tarball
  - [ ] Verify no remaining services depend on old VPS
  - [ ] Update DNS records, tear down VPS

### File Sync (Syncthing Replacement)

See <plans/file_sync_evaluation.md>.

## OVH-Only Resilience Invariants

**Rule**: These services MUST work with OVH only (Proxmox completely down). No
`proxmox-csi-retain` storage or Proxmox-pinned workloads.

| Service   | Status | Storage            | Notes                                                         |
| --------- | ------ | ------------------ | ------------------------------------------------------------- |
| DNS       | OK     | `local-path`       | CNPG on OVH/HIL nodes                                         |
| Website   | OK     | None (stateless)   |                                                               |
| Ingress   | OK     | None (hostNetwork) | Cilium Gateway on OVH                                         |
| Authentik | OK     | `local-path`       | All components pinned                                         |
| Grafana   | OK     | CNPG OVH-HA        | grafana-operator managed, JWT auth, no admin creds dependency |

**Compliance checklist** for critical-path changes:

1. No `proxmox-csi-retain` PVCs in dependency chain
2. No `topology.kubernetes.io/region: proxmox` affinity
3. Can schedule on OVH nodes
4. All upstream dependencies also pass 1-3

**Proxmox-dependent services** (tolerate downtime by design): Harbor,
Nix cache, BuildBuddy, Ollama, InvenTree, ActivityWatch.

### Migrating off `proxmox-csi-retain` on wyrm2

**Rationale**: Proxmox CSI hotplugs SCSI disks onto the VM. The bpg/proxmox Terraform
provider treats all disks as a single TypeSet with no stable keys — it can't distinguish
Terraform-managed disks from CSI-managed ones. This forces `lifecycle { ignore_changes = [disk] }`
on the entire VM, which means Terraform can't manage _any_ wyrm2 disks (including intentional
ones like cache disks). Eliminating proxmox-csi usage on wyrm2 lets us eventually remove the
ignore rule and manage all disks declaratively.

**Migration**: Replace `proxmox-csi-retain` PVCs with `local-path-proxmox` (same failure
domain, no CSI disk hotplug). Remaining `proxmox-csi-retain` consumers on wyrm2:
`ollama/llm-models` (200Gi), `devbot-workspace` (20Gi), `devbot-config` (5Gi).

## Operational Hardening

### Secrets: SOPS SSOT

All secrets are SOPS (age-encrypted in git, decrypted by Flux). ESO is still installed
but only with the Kubernetes provider, mirroring a small number of secrets cross-namespace
(authentik, openclaw-gateway/sandbox, openhands). Stakater Reloader restarts pods on
changes. Vault was decommissioned 2026-04-19 — see
<../archive/2026_04_19_vault_migration.md>.

### Google OAuth Client redirect URIs (blocked upstream)

The Google Cloud OAuth client backing Authentik's "Sign in with Google"
source (client_id `230253529789-…`, referenced from
`tf/gitops/sso-providers/source_google.tf`) is hand-managed in the
GCP Console. Every new forward-auth app needs its callback URI
(`https://<app>.allegedly.works/source/oauth/callback/google/`)
appended to the client's Authorized redirect URIs by hand — a
`redirect_uri_mismatch` is the symptom on first sign-in. The
behaviour itself is intentional in Authentik
(<https://github.com/goauthentik/authentik/issues/19883> closed as
not-planned); standalone proxy outposts run flows on the proxied
domain. Domain-Level forward-auth would centralise the callback but
sacrifices per-app group restrictions
(<https://docs.goauthentik.io/add-secure-apps/providers/proxy/forward_auth/>),
which is the entire reason for using forward-auth here.

**Why this isn't behind GitOps yet:** Web Application OAuth 2.0
Client IDs in GCP have no public CRUD API
(<https://issuetracker.google.com/issues/116182848>, filed 2018),
hence no Terraform resource
(<https://github.com/hashicorp/terraform-provider-google/issues/6074>).
`google_iap_client` is IAP-only; `google_iam_oauth_client` is
Workforce Identity Federation-only — neither covers the type of
client in use. Migrating to one of those products to unblock GitOps
is a much larger lift than the manual click.

Hit list when this changes:

- [ ] Watch <https://issuetracker.google.com/issues/116182848> for a
      public OAuth-client management API. When it lands and the
      Terraform provider gains a resource, move the client and its
      redirect URIs to TF.
- [ ] Until then: record per-app callback URI additions in the app's
      commit message so future audits can rebuild the list from git.

### Kyverno GitOps Enforcement

Deployed in Audit mode (`require-gitops` ClusterPolicy, 3 replicas).

- [ ] Switch to `Enforce` after validation
- [ ] Generic operator exclusion (skip resources with `ownerReferences`)
- [ ] Image registry allowlist (`ghcr.io`, `docker.io`, `registry.allegedly.works`, `quay.io`)

### Flux HelmRelease Drift Detection

By default Flux's helm-controller only runs `helm upgrade` when an HR's spec
changes, not on the periodic interval. A hand-edit like
`kubectl scale deployment ... --replicas=0` is invisible to it — the Helm
release is still recorded as "deployed" with the right values, the deployment
object still exists, so nothing looks drifted. Hit on 2026-05-24 when
`grafana-operator` had been hand-scaled to 0 for ~10 days without Flux
restoring it; grafana-deployment then sat at 0/0 because the operator
wasn't reconciling.

Per-HR opt-in fix:

    spec:
      driftDetection:
        mode: enabled

Enabled on `grafana-operator` (2026-05-24).

- [ ] Audit which HRs are safe to enable wider — anything where another
      controller legitimately mutates Helm-managed resources (HPA scaling,
      sidecar injectors, security defaulters, VPA on `Auto` mode) will
      fight drift correction and should stay off.

### Cilium Mutual Auth (SPIRE) -- Paused

SPIRE disabled — times out on Talos bootstrap. Nebula provides inter-node encryption.

- [ ] Investigate SPIRE timeout on Talos
- [ ] Once working: test-mode CiliumNetworkPolicies, then promote

### Firewall Hardening

Restrict K8s API (6443), Talos API (50000-50001), etcd (2379-2380), kubelet
(10250), Nebula (4242), and VXLAN (8472) to admin IPs and inter-node CIDRs.
Keep 80/443/53 public.

### Kubeconfig Endpoints (Current State)

| Consumer                      | Endpoint                    | Mechanism                            |
| ----------------------------- | --------------------------- | ------------------------------------ |
| Talos nodes (kubelet)         | `localhost:7445`            | KubePrism (built-in Talos API proxy) |
| NixOS workers (wyrm2, rugged) | `localhost:7445`            | haproxy -> all CP Nebula IPs         |
| TF state / `cluster/.envrc`   | `terraform/main/kubeconfig` | File written by OpenTofu             |
| `~/.kube/config` (wyrm2)      | `localhost:7445`            | Via local haproxy                    |

`api.allegedly.works` exists on port 443 behind the cluster Cilium Gateway
(TLSRoute in `k8s/kube-api-proxy/` with TLS passthrough to the `kubernetes`
Service). This is what Claude Code web sandboxes use to reach the k8s API —
Anthropic's egress proxy only allows port 443 outbound, so a non-standard port
would not work. TLS passthrough preserves client certificates for x509 auth.

### OpenTofu State Backend

All 6 former TF roots consolidated into a single root at `cluster/terraform/main/` with
PG backend (CNPG `tofu-state-db`, schema `main`, OVH local-path). Backup CronJob
removed 2026-06-01; will be restored via CNPG replication once wyrm2 is back.

All in-cluster tofu-controller `Terraform` CRs also use the PG backend (one schema per CR
in the same `tofu-state-db` cluster; reflector mirrors PG creds into `flux-system`).
PG advisory locks auto-release on runner-pod death — no more stale-Lease problem.

Zero `terraform_remote_state` dependencies — everything is in the same root. Persistent-auth
resources have `lifecycle { prevent_destroy = true }`. Bootstrap uses targeted applies
(`-target`) instead of separate directories. Single `proxmox` provider using
`PROXMOX_VE_API_TOKEN` env var (`root@pam`).

**Access**: From k8s workers (wyrm2, rugged), `.envrc` auto-detects ClusterIP and connects
directly — no port-forward. From non-workers, fall back to `kubectl port-forward`.

**Future**:

- [ ] Eliminate port-forward for non-workers. Cilium Gateway API does not support TCPRoute
      ([cilium#21929](https://github.com/cilium/cilium/issues/21929)). Options when available:
      TCPRoute + NodePort, or TLS passthrough (like `kube-api-proxy` TLSRoute pattern).

### GitHub Webhook Reconciliation

Flux `Receiver` at `flux-webhook.allegedly.works`. GitHub webhook registered by
`flux-webhook-token` Terraform (`github_repository_webhook.flux_receiver`).

### SOPS-encrypt talosconfig and kubeconfig

- [ ] Store `terraform/main/talosconfig.yml` and `terraform/main/kubeconfig` in SOPS
      instead of plaintext local files. These contain cluster admin credentials and
      should not be unencrypted on disk.

### etcd Backup

Deploy [talos-backup](https://github.com/siderolabs/talos-backup) CronJob with age
encryption to S3.

### InvenTree API Token Provisioning

Via `inventree-token-provisioner` Job. SOPS-managed sandbox-agent password ->
Job execs into pod, creates user via Django ORM -> `inventree-api-token` Secret
in `openclaw-sandbox` and `claude-sandbox`.

### CNPG Backup Strategy

Single-instance Proxmox CNPG clusters (inventree, harbor, props, matrix, tandoor) rely
on Proxmox ZFS for local reliability (checksums, snapshots). Off-site disaster recovery
needed:

- [ ] Generalize the `tofu-state` pg_dump CronJob pattern to all Proxmox CNPG clusters
      (write dumps to OVH-hosted PVC or object storage)
- [ ] Longer term: CNPG `ScheduledBackup` + Barman to S3-compatible store (SeaweedFS
      S3 gateway, or an external cloud bucket) for continuous WAL archiving and
      point-in-time recovery
- [ ] Verify Proxmox ZFS auto-snapshot schedule covers CNPG data directories

### Velero PVC Backup

Scheduled backups of PVCs (Harbor, Forgejo, Loki, Postgres). No backup strategy currently.

### CiliumNetworkPolicy Rollout

Most services lack network policies. Goal: default-deny per namespace.

**Done**: PowerDNS API, Authentik API, Prometheus, plus all proxy-backed services.

**Priority 2 -- Application services**:

- [ ] Harbor, Ollama, Grafana, Alertmanager, Forgejo, Tempo, Langfuse, Headlamp

**Priority 3 -- Remaining**:

- [ ] Cert-Manager, Metrics Server, ESO webhook, OpenClaw sandbox egress, Props, Nix cache

### Scoped Historical Logs for `claude-sandbox`

Agents in `claude-sandbox` can't reach Loki — `loki-ingress` CNP only allows
promtail/grafana/alloy/gatus/authentik, and Loki runs `auth_enabled: false` so the
CNP is the entire access boundary. Without log access, post-mortems on dead pods
(e.g. the 2026-05-24 `tana-mcp` livenessProbe kill) are limited to metrics +
kubelet's short-lived previous-container buffer.

Options, from cheapest to cleanest:

- **A. Allowlist `claude-sandbox` on `loki-ingress` CNP.** ~5 lines. Grants full
  cluster Loki read; scoping relies on the agent querying only namespaces it has
  business in (same trust model as `namespace-diagnostics-reader`).
- **B. `loki-agent-proxy` deployment in `monitoring`.** nginx + njs/Lua that
  rewrites incoming LogQL `query` params to inject `{namespace=~"<allowlist>"}`
  before forwarding to `loki-read:3100`. CNP grants `claude-sandbox` → proxy only,
  not Loki directly. Real per-namespace scoping. Mirrors existing precedent
  (`tana-mcp` nginx whitelisting `/mcp` + `/health`, `activitywatch-readonly`
  whitelisting `GET/POST /api/0/query`). Natural allowlist: the
  `namespace-diagnostics-reader` and `logs-configmaps-reader` binding sets in
  <../k8s/agents/agent-rbac-base/permissions.md>.
- **C. Loki multi-tenancy.** Set `auth_enabled: true`, route per-namespace logs to
  per-namespace tenants via Alloy/Promtail, grant tenant IDs. Touches every log
  producer; almost certainly overkill.

Grafana isn't a useful proxy here: OSS Grafana's Loki datasource has no
label-level access controls (TeamLBAC is Enterprise-only), so routing through
Grafana shifts the trust boundary without actually scoping access.

Recommendation: **B** when log access starts mattering for routine triage; **A**
as a stop-gap if the trust-the-agent model is acceptable.

### Pod Security Standards

Apply `restricted` PSS labels to app namespaces. System namespaces keep `privileged`.
Start `warn`, promote to `enforce`.

### Scheduling Priorities

Motivated by 2026-03-17 OOM cascade. Deploy PriorityClasses: `system-critical`
(DNS/ingress/Authentik), `important` (Forgejo/Harbor/monitoring), `batch`
(OpenClaw/props/BuildBuddy). Plus Descheduler, PDBs, ResourceQuota + LimitRange.

**TODO (basic-infra reliability sweep)**: do a deliberate pass over the core
stateful/infra workloads and check whether more tuning is warranted — don't wait
for the next incident. Surfaced by the 2026-06-19 SeaweedFS descheduler→DNS
crash-loop (see <lessons_learned/2026_06_19_seaweedfs_descheduler_dns_race_crashloop.md>),
which exposed cluster-wide BestEffort hygiene gaps:

- **BestEffort is cluster-wide, not SeaweedFS-only.** Every operator-managed
  stateful pod with no resource config is BestEffort — notably **all CNPG DB
  pods** (props-db, langfuse-db, authentik, forgejo, grafana, matrix, …) and the
  CNPG operator itself. They survive the descheduler only via CNPG's auto-PDB;
  they're still first OOM/node-pressure victims. Decide on a CNPG `resources`
  convention (see <cnpg_conventions.md>) so DBs leave BestEffort.
- **Generalize the `stateful-infra` PriorityClass** (added for SeaweedFS in
  `k8s/seaweedfs/cluster/priorityclass.yaml`, value 1_000_000) into the tiered
  set above, and apply it to the stateful workloads that need it.
- **Review the descheduler config** (`k8s/descheduler/helmrelease.yaml`):
  `LowNodeUtilization` is metrics/usage-based and evicts BestEffort first;
  consider `ignorePvcPods` and/or requests-based utilization so transient CPU
  bursts (e.g. a runaway agent) don't trigger eviction of stateful pods.
- **SeaweedFS DNS init-wait (deferred §E in the RCA note):** the operator binds
  `-ip=<self-FQDN>` and fatal-crashes if it doesn't resolve at startup; an
  init-wait sidecar would harden against _involuntary_ restarts (node crash,
  OOM, drains) that the QoS/PDB fix doesn't cover.

Broaden from there: walk the rest of basic infra (CNI, CoreDNS HA, storage
provisioners, control-plane etcd, ingress) and note anywhere a single transient
fault can cascade.

### VPA + Goldilocks

VPA deployed (`k8s/vpa/`). Goldilocks auto-creates VPAs cluster-wide.
Default mode "Off" (recommendation-only). Enable per namespace.

**TODO**: Require explicit `goldilocks.fairwinds.com/vpa-resource-policy` annotations
on all namespaces/deployments that use `updateMode: auto`. Without a policy:

- VPA can recommend absurdly low CPU (e.g., 15m) that causes throttling.
  Consider a Kyverno policy to enforce this cluster-wide. See airlock namespace
  for working example (JSON format required, not YAML).

**CPU limits policy**: for workloads with expensive cold starts (Python/JVM),
use `"controlledValues":"RequestsOnly"` so VPA only sets CPU requests and never
adds a CPU limit. CFS CPU limits are a hard rate limiter enforced by cgroups
regardless of node load — a pod capped at 60m gets 60ms/s of CPU even on a
completely idle node. Removing the CPU limit lets cold-start bursts use idle
capacity; when the node is contended, CFS shares CPU proportionally to requests
(compressible resource — no pod is killed). Memory limits remain useful
(`RequestsAndLimits`) since memory is incompressible.

See `cluster/k8s/agents/tana-mcp-facade/deployment.yaml` for a working example
(fastmcp takes ~6 CPU-seconds to import; at 60m limit this costs 100s wall time).

### Alertmanager -> ntfy Bridge

Deploy [alertmanager-ntfy](https://github.com/alexbakker/alertmanager-ntfy).

### SLO Definitions

Deploy Pyrra or Sloth. Targets: ingress 99.5%, DNS 99.9%, Authentik 99.5%.

### GUI for Terraform/OpenTofu

Evaluate a web UI for managing cluster infrastructure via TF/OpenTofu — plan/apply from
a dashboard instead of CLI. Candidates: Spacelift (SaaS), env0, Atlantis (self-hosted,
PR-based), Terrateam, Scalr, or the OpenTofu-native options. Self-hosted preferred.

### Tekton / Argo Workflows for in-cluster CI

Evaluate Tekton Pipelines or Argo Workflows for in-cluster automations that need
to write back to git (e.g., JWT rotation, secret rotation, image pin updates).
Currently these are ad-hoc CronJobs with sparse clones and `git push`. A
lightweight workflow engine would give structured retries, DAG dependencies,
artifact passing, and a UI for debugging failed runs.

## Future Directions

### GPU: DRA

DRA replaces device plugins for GPU exposure. Still beta.
See <https://docs.siderolabs.com/kubernetes-guides/advanced-guides/dynamic-resource-allocation>

### GPU: KubeRay, Kueue

KubeRay for distributed ML, Kueue for job quota management on GPU node.

### Shared Docker Daemon for CI Container Tests

Container integration tests spend ~46s per run loading OCI images (376MB total:
mitmproxy 254MB, two custom ~118MB images sharing a 113MB Python interpreter layer)
into Docker on disposable RBE Firecracker VMs. A persistent Docker daemon with cached
layers would make subsequent loads near-instant.

- [ ] Drop bazelisk wrapper in favor of `bb` CLI (embeds bazelisk + reads `BUILDBUDDY_API_KEY`)

### Self-Hosted Bazel Remote Cache

Legacy VPS cache removed. If needed again, deploy in-cluster on Proxmox storage.

### Service Mesh

Not needed while Cilium mutual auth + Gateway API suffice. Options if needed:
Cilium Service Mesh (eBPF L7, no sidecars) or Istio ambient mode.

### Database HA

CNPG is 2-instance primary+standby. Low priority -- survives single-node failure.

### Future Services

- [ ] Jellyfin, \*arr stack, Paperless-ngx, File sync, Tetragon

### Low Priority

- `BackendTLSPolicy` for internal HTTPS (blocked on [cilium#31352](https://github.com/cilium/cilium/issues/31352))
- Nix cache signing key -> GitOps Terraform writing a SOPS secret
- RWX shared storage: Longhorn NFS export (try first), NFS on Proxmox, JuiceFS, SeaweedFS

## Architecture Decisions

### CNI: Cilium with VXLAN

VXLAN tunnel mode (UDP 8472). OVH and Proxmox nodes are not on the same L2; native
routing fails. The full network layering and MTU model (pod 1370 → Cilium VXLAN →
`nebula1` 1420 → `eno1` 1500, including the Cilium-`MTU`-is-underlay gotcha) lives in
<network.md>.

### Storage Strategy

Use OVH-local storage for public-critical services and Proxmox storage for
storage-heavy services that tolerate home downtime.

| Location | Services                                                               | Rationale                         |
| -------- | ---------------------------------------------------------------------- | --------------------------------- |
| OVH      | Authentik, Grafana, Gateway, DNS, cert-mgr                             | Always-on, critical path          |
| Home     | Harbor, Ollama                                                         | Storage-heavy, tolerates downtime |
| OVH      | SeaweedFS, attic-db, Forgejo, Nix cache chunks + Loki/Mimir/Tempo (S3) | Replicated across 2 kimsufi nodes |

CNPG: individual clusters per app. Two profiles: OVH-HA (2 instances, OVH
kimsufi) and Proxmox-single (1 instance). See <cnpg_conventions.md>.

## Related Documentation

- <bootstrap.md>
- <troubleshooting.md>
- <lessons_learned/2025_11_28_eso_password_generator_desync.md>

**Monthly Cost**: OVH Kimsufi bare metal (3× KS-5 + 2× KS-GAME, HIL); refresh figure (was ~EUR64/mo on the retired Hetzner CPX31 fleet).
