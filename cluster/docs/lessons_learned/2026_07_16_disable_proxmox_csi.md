# Disabling Proxmox CSI (2026-07-16)

## What was removed

The Proxmox CSI driver (`csi.proxmox.sinextra.dev`) and its `proxmox-csi-retain`
StorageClass. Removed `cluster/k8s/proxmox-csi/`, its entries in the root
`cluster/k8s/kustomization.yaml`, the `proxmox-csi` `dependsOn` on the `ollama`
Kustomization, the `proxmox-csi-retain` CDI StorageProfile, and the
`csi.proxmox.sinextra.dev/max-volume-attachments` Proxmox node label. Flux prunes
the live chart (CSIDriver, StorageClass, controller Deployment, node DaemonSet,
ClusterRoles, `csi-proxmox` namespace) on reconcile.

## Why

**It flapped because the topology moved out from under it.** The controller reaches
the Proxmox API to provision/attach disks. That path used to work when the cluster
and the Proxmox host shared the old home network — the controller was effectively
talking to `atlas` over the home LAN rather than over PVE-host <-> VM internal
networking. After the network topology changed, that route no longer exists, so
every `GetCapacity`/attach call to `https://10.2.0.2:8006` times out and the
controller sidecars crash-loop.

**We don't need it.** There is a single physical Proxmox host, so a CSI volume
hotplugged onto a wyrm2 VM has exactly the same failure domain as a local LVM
volume inside that VM — one host, no cross-node replication or durability either
way. The OpenEBS LVM provisioner (`lvm-proxmox-hdd` et al.) and `local-path-proxmox`
give the same durability with none of the Proxmox-API dependency. At removal time no
PV, PVC, or VolumeAttachment used the driver — the only remaining consumers on paper
(`ollama/llm-models`, `devbot`) had already moved to `lvm-proxmox-hdd`, and devbot's
PVCs were repointed to `lvm-proxmox-hdd` here.

**Bonus: it unblocks Terraform disk management.** Proxmox CSI hotplugs SCSI disks
onto the VM; the bpg/proxmox provider treats all disks as one keyless TypeSet and
can't tell Terraform-managed disks from CSI-managed ones, forcing
`lifecycle { ignore_changes = [disk] }` on the whole wyrm2 VM. Dropping CSI lets us
eventually remove that ignore rule and manage all wyrm2 disks declaratively.

## Replacement

Durable-ish wyrm2-local storage: `lvm-proxmox-hdd` / `lvm-proxmox-ssd` (OpenEBS LVM)
or `local-path-proxmox`. Same single-host failure domain, no CSI disk hotplug.
