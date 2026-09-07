# KubeVirt

KubeVirt and CDI are installed from pinned upstream release manifest URLs:

- KubeVirt `v1.8.2`
  - source: <https://github.com/kubevirt/kubevirt/releases/download/v1.8.2/kubevirt-operator.yaml>
  - sha256: `57e0822062e6617964fd7e1731f9ace531f8ee34a0e48cf8dd0ccce72196e1d6`
- CDI `v1.65.0`
  - source: <https://github.com/kubevirt/containerized-data-importer/releases/download/v1.65.0/cdi-operator.yaml>
  - sha256: `e96d59abdf358c5161cb96adcfdcc6107efc3fb608ec93ade11578c94a222015`

The local kustomizations reference the release URLs directly to avoid vendoring
large generated upstream YAML. The hashes above are recorded so URL contents can
be checked during upgrades.

Workloads are constrained to non-control-plane OVH/Talos/Proxmox workers via
`workloads.nodePlacement` region affinity in `app/kubevirt.yaml`. `wyrm2`
(region `proxmox`) is a nested-KVM-capable Proxmox guest — confirmed via
`kvm_amd` `nested=1` on both `atlas` (L0) and `wyrm2` (L1) — and included in
that affinity.
