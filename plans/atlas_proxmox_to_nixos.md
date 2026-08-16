# Atlas: Proxmox → NixOS Migration Delta

Written 2026-04-19 and **not started since**: `atlas` still runs Proxmox
(<../cluster/README.md> node table), `ansible/atlas.yaml` still configures it, and
`cluster/terraform.tf` still pins `bpg/proxmox`. The delta analysis below is the
value here — it is what would otherwise be re-derived — not a scheduled piece of
work. The friction it exists to remove is still real and still recorded elsewhere:
<../nix/TODO.md> has to special-case atlas for `services.google-drive` because it is
not NixOS.

## Current state

Atlas runs Proxmox VE 9.1.7 (Debian trixie) with kernel 6.17.13-2-pve.
Two running VMs: wyrm2 (NixOS workstation + GPU worker, 2x RTX 5090 VFIO passthrough)
and talos-pve-cp-0 (Talos k8s control plane). Several stopped VMs (wyrm, win11 template,
windows11-test, linux-desktop-01, ubuntu cloud-init template). ZFS pools: rpool (3.6T)
and tank (58T). No LXC containers, no Ceph, no PBS, PVE firewall disabled.

## Actual losses

### Proxmox Web UI

The main loss. Used for VM lifecycle management and SPICE console access (exposed via
Authentik SSO at `atlas.allegedly.works`). Replacements:

- **`virt-manager`** — GTK app, full VM management, already familiar UX. No web access.
- **`cockpit-machines`** — Web UI for libvirt VMs. Less polished than PVE but functional.
  NixOS has `services.cockpit`.
- **`virsh` CLI** — Always available, scriptable.

### Terraform provider (`bpg/proxmox` → `dmacvicar/libvirt`)

VM infrastructure is declared in Terraform against the PVE API. Needs rewrite to the
`libvirt` provider. Both providers support:

- VM creation with CPU/memory/disk config
- Cloud-init
- Network bridge attachment
- Disk image management

The libvirt provider doesn't have PVE-specific features (templates, clones from PVE
storage) but those can be replaced with ZFS snapshots + cloud-init.

Alternative: declare VMs purely in NixOS config via `virtualisation.libvirtd` +
`systemd.services` for QEMU, eliminating Terraform for VMs entirely.

### `proxmox_vm` skill (screenshots)

Currently uses PVE API for QEMU monitor screenshots. Replace with `virsh screenshot`
or direct QEMU monitor protocol (`qmp-shell`). Straightforward adaptation.

### Authentik SSO proxy for PVE web UI

`proxmox-sso.yaml` Authentik blueprint routes `atlas.allegedly.works` to the PVE web UI.
Would need to point at whatever replacement web UI is chosen (cockpit, or remove entirely).

## No real loss

| Feature                           | Why                                              |
| --------------------------------- | ------------------------------------------------ |
| PVE HA manager                    | Single node, not used                            |
| PVE cluster filesystem (`pmxcfs`) | Single node, `/etc/pve/` not needed              |
| SPICE proxy                       | Direct `remote-viewer` to QEMU socket works fine |
| PVE firewall                      | Currently disabled                               |
| Ceph                              | Not configured                                   |
| PBS / vzdump backups              | Not configured                                   |
| LXC containers                    | None running                                     |
| PVE SDN                           | Not configured                                   |
| Corosync                          | Single node                                      |
| Proxmox CSI driver                | Already removed — see below                      |

The Proxmox CSI driver (`csi.proxmox.sinextra.dev`) and its `proxmox-csi-retain`
StorageClass were the one item on the original loss list that has since been dealt
with, and for reasons unrelated to this migration: the per-node volume-attachment cap
forced the move to OpenEBS LVM in July 2026. See
<../cluster/docs/lessons_learned/2026_07_16_disable_proxmox_csi.md>. Nothing depends
on the PVE API for storage any more.

## Direct equivalents (no migration work beyond NixOS config)

| Proxmox feature            | NixOS equivalent                             |
| -------------------------- | -------------------------------------------- |
| KVM/QEMU (`pve-qemu-kvm`)  | `virtualisation.libvirtd` + `qemu` package   |
| ZFS (`local-zfs`, `tank`)  | `boot.zfs.extraPools`, first-class support   |
| VFIO GPU passthrough       | Same kernel modules, libvirt XML `<hostdev>` |
| Bridges (`vmbr0`, `vmbr4`) | `networking.bridges` / `networking.vlans`    |
| virtiofs mounts            | `virtiofsd` systemd units + QEMU args        |
| SPICE audio/USB            | QEMU `-spice`, `-device ich9-intel-hda`      |
| Kernel cmdline tuning      | `boot.kernelParams`                          |
| zram swap                  | `zramSwap.enable = true`                     |
| Cloud-init snippets        | Files on disk, same mechanism                |
| Subscription nag patch     | Not needed on NixOS                          |

## Gains

- **Declarative host config** — atlas joins the NixOS fleet (agentydragon, gpd, wyrm2)
  instead of being the odd Debian machine managed by Ansible
- **Atomic upgrades with rollback** — vs `apt upgrade` on Debian
- **Ansible elimination** — `ansible/atlas.yaml` becomes a NixOS module; kernel cmdline,
  VFIO modules, udev rules, zram, all in one config
- **Unified dotfiles** — home-manager already used, but NixOS system config is more
  consistent than Ansible on Debian
- **No subscription nag** — no more JS patching

## Migration order (sketch)

1. Write NixOS config for atlas (ZFS root, networking, libvirtd, VFIO, kernel params)
2. Port Terraform VM definitions from `bpg/proxmox` to `dmacvicar/libvirt`
3. Test VFIO passthrough + virtiofs in libvirt (the trickiest part — RTX 5090 D3cold
   workarounds, `cache=never`, etc.)
4. Install NixOS on atlas (ZFS pools preserved, just swap root)
5. Remove Proxmox-specific infra: Terraform provider, Ansible playbook, Authentik
   blueprint
