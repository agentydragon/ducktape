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

Prerequisites (credentials and access): <docs/bootstrap.md#prerequisites>. direnv in
`cluster/`: its `.envrc` exports `KUBECONFIG`/`TALOSCONFIG` and provides `talosctl`,
`kubectl`, `get-passwords` (service credentials).

## Infrastructure

- Network `10.2.0.0/16` (VLAN 4 on Proxmox `vmbr4`): `10.2.0.2` atlas (reachable only
  from the Proxmox VLAN). The only cluster node in the Proxmox region is `wyrm2`
  ([nodes](docs/topology.md#nodes)).
- Domain `*.allegedly.works`: AWS Route 53, DNS-01, dual LE issuers. Kubeconfig points at
  `api.allegedly.works`.
- HTTPS: Internet → OVH bare metal:443 → Cilium Envoy → pods.
- Nebula mesh (UDP 4242, lighthouses + relays on OVH). MTU: pod 1370 → VXLAN → `nebula1`
  1420 → `eno1` 1500 (<docs/network.md>).
- Mesh roster (every Nebula peer, incl. non-k8s `atlas`, `pixel6`): `nebula-mesh.json` at
  repo root (SSOT); add/remove/re-IP via <docs/mesh_membership.md>.

### Nodes

Inventory: [nodes](docs/topology.md#nodes).

Region is `topology.kubernetes.io/region`. Never schedule workloads needing persistent
availability on roaming nodes; the laptops are tainted
`node-role.kubernetes.io/roaming=true:NoSchedule`, and marked out of service while offline
(<roaming_node_controller/README.md>), so DaemonSets need no roaming-specific rollout
budget or Helm `disableWait`. A DaemonSet that tolerates every taint still counts an
offline laptop.

GPU: wyrm2 provides `nvidia.com/gpu`; GPU pods need `runtimeClassName: nvidia`
(<docs/gpu.md>).

## Services

Service inventory: [curated services](docs/topology.md#services).

To find checked-in `HTTPRoute` manifests, run this from the repository root.

```bash
git grep -l '^kind: HTTPRoute$' -- cluster/generated cluster/k8s
```

SSO: apps use Authentik via Terraform-managed OIDC/proxy providers plus a shrinking set of
blueprint-managed proxy providers (<docs/sso.md>). ActivityWatch: per-device importers push
into a central aw-server (<docs/activitywatch/README.md>).

## Storage

All storage is region-local (no cross-site synchronous replication). Curated; SSOT is the
`StorageClass` manifests (e.g. `generated/{local-path-provisioner,openebs-lvm}/`) plus CSI
Helm values.

Storage-class inventory: [storage](docs/topology.md#storage).

CNPG placement (OVH-HA vs Proxmox-single): <docs/cnpg_conventions.md>.

## Failure modes

Failure-mode inventory: [cluster failure modes](docs/topology.md#failure-modes).

### OVH-Only Resilience Invariants

DNS (Route 53) and the public website must keep working and recover with OVH only, so they
must not depend on Proxmox-pinned storage (`lvm-proxmox-*`, `local-path-proxmox`) or
nodes. Full set and checklist: <docs/decisions.md> § "OVH-Only Resilience Invariants".

## In-cluster Terraform (tofu-controller)

`Terraform` CRs (`tf/gitops/*`, infra-drift) use the upstream tofu-controller chart and
image, with `ExternalArtifact` enabled. Each module reads its own content-addressed
`ArtifactGenerator` artifact (`cdk8s/terraform.py`, <cdk8s/infra_drift/README.md>), so
only changed modules replan. Flux applies the chart release's CRD separately, since Helm
does not upgrade CRDs from the chart's `crds/` directory.

The controller uses our `ghcr.io/agentydragon/tf-runner`, an upstream runner with all pinned
providers baked in (<tf_runner/README.md>). Its upstream base must match the controller's
release and their rollout must be coordinated. Both images come from GHCR, not Forgejo:
the Forgejo pull credential is itself created by a Terraform CR.

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
├── parked/        # retained third-party app manifests; usually suspended, with explicit cleanup exceptions
├── rotators/      # credential-rotation CronJob image source
├── provisioners/  # reconciler CronJob/Job image source
├── proxies/       # long-running proxy image source (loki-read-proxy)
└── validation/    # structural validation tests
```

## Let's Encrypt rate limits

5 duplicate certs/week per domain (rolling); every destroy→bootstrap cycle requests fresh
certs. Active issuer: `LETSENCRYPT_ISSUER` in `cdk8s/cert_manager/config.py`.
