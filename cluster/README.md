# Talos Kubernetes Cluster

Small Talos k8s cluster with GitOps and HTTPS.

- Deploy: `bazel run //cluster:bootstrap` (automated layered deployment; setup:
  <docs/bootstrap.md>)
- Machines: Talos on OVH Kimsufi bare metal plus NixOS/Proxmox workers, configured with
  OpenTofu
- Ingress: Cilium Gateway API (Envoy hostNetwork on every Cilium node; public DNS → OVH)
- CNI: Cilium VXLAN (infrastructure-managed, not GitOps)
- Secrets: SOPS (age, decrypted by Flux); ESO's Kubernetes provider mirrors a few
  cross-namespace. No Vault (<docs/decisions.md> § "Secrets: SOPS SSOT").

Prerequisites: SSH to Proxmox host `root@atlas`, OVH API credentials
(`secrets/ovh-credentials.sops.yaml`), `gh auth login` (Flux), direnv in `cluster/` —
its `.envrc` exports `KUBECONFIG`/`TALOSCONFIG` and provides `talosctl`, `kubectl`,
`get-passwords` (service credentials).

## Infrastructure

- Network `10.2.0.0/16` (VLAN 4 on Proxmox `vmbr4`): `10.2.0.2` atlas (reachable only
  from the Proxmox VLAN), `10.2.1.x` control plane, `10.2.2.x` workers (Proxmox).
- Domain `*.allegedly.works`: AWS Route 53, DNS-01, dual LE issuers. Kubeconfig points at
  `api.allegedly.works`.
- HTTPS: Internet → OVH bare metal:443 → Cilium Envoy → pods.
- Nebula mesh (UDP 4242, lighthouses + relays on OVH). MTU: pod 1370 → VXLAN → `nebula1`
  1420 → `eno1` 1500 (<docs/network.md>).
- Mesh roster (every Nebula peer, incl. non-k8s `atlas`, `pixel6`): `nebula-mesh.json` at
  repo root (SSOT); add/remove/re-IP via <docs/mesh_membership.md>.

### Nodes

| Node                                           | Type             | Region    | Availability     | Hardware                         |
| ---------------------------------------------- | ---------------- | --------- | ---------------- | -------------------------------- |
| `ovh-ns104952`, `ovh-ns104963`                 | Talos CP         | `hil`     | Always on        | OVH KS-GAME                      |
| `ovh-ns1001419`                                | Talos CP         | `hil`     | Always on        | OVH SYS-1, 64 GiB, 2x512 GB NVMe |
| `ovh-ns103656`, `ovh-ns103711`, `ovh-ns102453` | Talos worker     | `hil`     | Always on        | OVH Kimsufi KS-5                 |
| `optiplex`                                     | Talos worker     | `home`    | Always on (home) | Dell OptiPlex 7060               |
| `wyrm2`                                        | NixOS GPU worker | `proxmox` | Always on (home) | 2x RTX 5090                      |
| `iguana`                                       | NixOS laptop     | `roaming` | Often offline    | ThinkPad X1 Extreme              |
| `rugged`                                       | NixOS laptop     | `roaming` | Often offline    | Dell Rugged 12                   |

Region is `topology.kubernetes.io/region`. Never schedule workloads needing persistent
availability on roaming nodes; `rugged` is tainted
`node-role.kubernetes.io/roaming=true:NoSchedule`. Changing the roaming-node count
requires raising `maxUnavailable` on DaemonSets scheduling onto them
(`//cluster/cdk8s/monitoring:test_roaming_daemonset_capacity` fails otherwise;
<docs/mesh_membership.md> § Roaming k8s nodes).

GPU: wyrm2 provides `nvidia.com/gpu`; GPU pods need `runtimeClassName: nvidia`
(<docs/gpu.md>).

## Services

Curated; the full set is the HTTPRoutes under `k8s/` and `k8s/authentik/proxy-routes/`.

| Service        | URL                               | Purpose                 |
| -------------- | --------------------------------- | ----------------------- |
| Authentik      | <https://auth.allegedly.works>    | SSO                     |
| Forgejo        | <https://git.allegedly.works>     | Git hosting             |
| Matrix/Element | <https://chat.allegedly.works>    | Chat                    |
| Grafana        | <https://grafana.allegedly.works> | Monitoring              |
| Nix Cache      | <https://cache.allegedly.works>   | Binary cache            |
| Gatus          | <https://status.allegedly.works>  | Health monitoring       |
| Ollama         | <https://ollama.allegedly.works>  | LLM inference (GPU)     |
| Airlock        | <https://airlock.allegedly.works> | OAuth credential broker |

SSO: apps use Authentik via Terraform-managed OIDC/proxy providers plus a shrinking set of
blueprint-managed proxy providers (<docs/sso.md>). ActivityWatch: per-device importers push
into a central aw-server (<docs/activitywatch/README.md>).

## Storage

All storage is region-local (no cross-site synchronous replication). Curated; SSOT is the
`StorageClass` manifests (e.g. `generated/{local-path-provisioner,openebs-lvm}/`) plus CSI
Helm values.

| StorageClass          | Provisioner            | Region    | Notes                                                                                          |
| --------------------- | ---------------------- | --------- | ---------------------------------------------------------------------------------------------- |
| `seaweedfs-ovh`       | SeaweedFS CSI          | `hil-ovh` | **Default for app data**; not node-pinned; POSIX/S3 (HDD)                                      |
| `seaweedfs-ovh-ssd`   | SeaweedFS CSI          | `hil-ovh` | NVMe SeaweedFS (`diskType: ssd`), Forgejo git hot tier                                         |
| `local-path-proxmox`  | local-path-provisioner | `proxmox` | Proxmox-single CNPG DBs; node-pinned Proxmox app data                                          |
| `local-path-ovh-hdd`  | local-path-provisioner | `hil-ovh` | KS-5 HDD: SeaweedFS `hdd` volume servers, OVH-HA CNPG DBs, node-pinned bulk data               |
| `local-path-ovh-ssd`  | local-path-provisioner | `hil-ovh` | OVH NVMe: SeaweedFS `ssd` volume servers, SSD-pinned DBs                                       |
| `local-path-home-ssd` | local-path-provisioner | `home`    | OptiPlex NVMe: LAN/hardware-pinned home state (Home Assistant); VolSync copies it to SeaweedFS |
| `local-path-ovh`      | local-path-provisioner | `hil-ovh` | Deprecated alias of `local-path-ovh-hdd`                                                       |
| `lvm-proxmox-ssd`     | OpenEBS LVM CSI        | `proxmox` | NVMe thin; nodes with VG `openebs-proxmox-ssd`                                                 |
| `lvm-proxmox-hdd`     | OpenEBS LVM CSI        | `proxmox` | HDD thin; nodes with VG `openebs-proxmox-hdd`                                                  |

CNPG placement (OVH-HA vs Proxmox-single): <docs/cnpg_conventions.md>.

## Failure modes

| Scenario              | Cluster    | Ingress/DNS/Authentik | Notes                                             |
| --------------------- | ---------- | --------------------- | ------------------------------------------------- |
| Single OVH CP down    | 2/3 quorum | Work                  | Surviving OVH CP carries ingress + lighthouse     |
| Multiple OVH CPs down | 1/3        | Down                  | Home pods continue; cluster frozen                |
| Home down             | 3/3 quorum | Work                  | Public-critical services run on OVH-local storage |

### OVH-Only Resilience Invariants

DNS (Route 53) and the public website must keep working and recover with OVH only, so they
must not depend on Proxmox-pinned storage (`lvm-proxmox-*`, `local-path-proxmox`) or
nodes. Full set and checklist: <docs/decisions.md> § "OVH-Only Resilience Invariants".

## In-cluster Terraform (tofu-controller)

`Terraform` CRs (`tf/gitops/*`, infra-drift) run on two custom GHCR images (not Forgejo:
the Forgejo pull credential is created by one of those CRs):

- `ghcr.io/agentydragon/tofu-controller`: upstream plus an `ExternalArtifact` source kind
  (fork branch `external-artifact-source`, <../third_party/tofu_controller/README.md>), so
  each module reads its own content-addressed `ArtifactGenerator` artifact
  (`cdk8s/terraform.py`, <cdk8s/infra_drift/README.md>) and only changed modules replan.
  Drop once upstream releases flux-iac/tofu-controller#1901.
- `ghcr.io/agentydragon/tf-runner`: upstream runner with all pinned providers baked in
  (<tf_runner/README.md>); its base tag must match the controller fork's base release.

## Layout

```text
cluster/
├── .envrc         # KUBECONFIG, TALOSCONFIG; CLI tools from root devShell
├── docs/
├── cdk8s/         # Python generators for manifests
├── terraform/main/  # single TF root (PG backend)
├── generated/     # Flux dirs cdk8s writes in full (same sub-paths as k8s/; never edit)
├── k8s/           # Flux dirs with a hand-written file; config only
│   ├── <service>/   # flat (one Flux Kustomization) or grouped (subdir per Kustomization)
│   └── flux/flux-system/  # Flux controllers (auto-generated)
├── parked/        # suspended/decommissioned third-party apps, nothing applies them
├── rotators/      # credential-rotation CronJob image source
├── provisioners/  # reconciler CronJob/Job image source
├── proxies/       # long-running proxy image source (loki-read-proxy)
└── validation/    # structural validation tests
```

## Let's Encrypt rate limits

5 duplicate certs/week per domain (rolling); every destroy→bootstrap cycle requests fresh
certs. Active issuer: `LETSENCRYPT_ISSUER` in `cdk8s/cert_manager/config.py`.
