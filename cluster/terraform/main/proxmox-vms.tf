# Wyrm2 — NixOS dev workstation + k8s GPU worker on Proxmox

locals {
  repo_root = "${path.module}/../../.."
}

# NIXOS BOOTSTRAP IMAGE

# Bootstrap NixOS qcow2 image — minimal SSH-able image for initial VM provisioning.
# Only rebuilt when rebuild_image=true. After boot, nixos-rebuild deploys the full config.
module "wyrm2_image" {
  source        = "../../../terraform/modules/nixos-image"
  flake_target  = "bootstrap"
  proxmox_host  = var.proxmox_node_name
  repo_root     = local.repo_root
  build_enabled = var.rebuild_image
}

# VM INSTANCE

resource "proxmox_virtual_environment_vm" "wyrm2" {
  name        = "wyrm2"
  description = "NixOS VM - wyrm2"
  node_name   = var.proxmox_node_name
  vm_id       = 110
  bios        = "ovmf"
  machine     = "q35" # Required for PCIe passthrough

  # Terraform runs from inside wyrm2. Stage reboot-required VM changes in
  # Proxmox, then activate them with the coordinated Atlas host reboot instead
  # of terminating the machine that is still writing Terraform state.
  reboot_after_update = false
  on_boot             = true

  cpu {
    cores = 32
    type  = "host"
    numa  = true # Required by Proxmox for hugepage-backed memory
  }

  memory {
    # Keep wyrm2 at 96 GiB, but back it with the 1 GiB hugepage pool reserved by
    # ansible/atlas.yaml. This removes guest RAM from normal host reclaim while
    # leaving the rest of Atlas's memory explicitly available to the host/ARC.
    dedicated      = 98304
    floating       = 0 # Disable balloon (VFIO incompatible)
    hugepages      = "1024"
    keep_hugepages = true
  }

  # GPU passthrough. hostpci0 → guest 01:00.0 (first PCI), hostpci1 → guest
  # 02:00.0. The DISPLAY GPU must be guest 01:00.0 because DXVK renders on the
  # first-PCI device and the two 5090s are identical (no per-app selector) — so
  # the monitor's card has to be hostpci0 to avoid a cross-PCIe copy per frame.
  # See nix/docs/wyrm2_display.md.
  #
  # Display 5090 (host 01:00, IOMMU group 14; DP cable to the FV43U): pass the
  # WHOLE device (no function suffix) so the guest gets both the GPU (01:00.0)
  # and its DP/HDMI audio function (01:00.1) — the monitor's audio path for the
  # gaming seat. Applied imperatively via qm on atlas; this keeps TF in sync.
  hostpci {
    device = "hostpci0"
    id     = "0000:01:00"
    pcie   = true
    rombar = true
  }
  # Compute 5090 (host 03:00): headless, no monitor. Whole device passed (its
  # audio function 02:00.1 rides along harmlessly).
  hostpci {
    device = "hostpci1"
    id     = "0000:03:00"
    pcie   = true
    rombar = true
  }

  audio_device {
    device  = "ich9-intel-hda"
    driver  = "spice"
    enabled = true
  }

  usb {
    host = "spice"
    usb3 = true
  }
  # Atlas's front-panel USB-C port (host path 3-1), passed through by physical
  # port so whatever device is plugged in is presented to wyrm2 as Proxmox usb1.
  usb {
    host = "3-1"
    usb3 = true
  }
  # RTL-SDR Blog V4 (Realtek RTL2838) — for SDR streaming via rtl_tcp.
  usb {
    host = "0bda:2838"
    usb3 = true
  }
  # TEX Shura keyboard via the FV43U monitor hub's USB-B uplink → atlas rear
  # USB-A port (bus 3 port 2), passed by PORT PATH through the hub (QEMU can't
  # pass hubs themselves): grabbed only while the monitor's KVM routes its hub
  # to USB-B; on the USB-C/KVM side the path is empty and atlas keeps the
  # keyboard. Feeds wyrm2's local logind seat0 (direct-display gaming) — see
  # nix/docs/wyrm2_display.md. Update the path if the cable
  # moves to another rear port.
  usb {
    host = "3-2.1"
    usb3 = true
  }
  # Logitech C920 webcam via the sibling port on the same FV43U monitor hub.
  # Like the keyboard, this child device is present only while the monitor's
  # KVM routes the hub to USB-B; atlas owns the hub in USB-C/KVM mode.
  usb {
    host = "3-2.2"
    # The VM's xHCI root ports are full; use the free high-speed EHCI port
    # rather than placing this USB 2.0 camera behind the full-speed hub.
    usb3 = false
  }
  hotplug = "network,disk,cpu,usb" # note: memory hotplug requires NUMA

  # virtio-gl (VirGL): guest GL runs on atlas's iGPU, so Mutter composites
  # with GPU acceleration even when the NVIDIA GPUs are locked up or display-
  # less (plain virtio fell back to llvmpipe → choppy SPICE audio — see
  # nix/debug/wyrm2/spice_audio/README.md).
  vga {
    type   = "virtio-gl"
    memory = 256
  }

  # cache=never: virtiofsd with cache=auto leaks memory — it caches all accessed
  # files with no eviction, growing to 10+ GiB over days. On a 128 GiB host with
  # 96 GiB pinned for this VM, that starves ZFS ARC and causes system-wide stalls.
  virtiofs {
    mapping = "tankshare"
    cache   = "never"
  }
  virtiofs {
    mapping = "code"
    cache   = "never"
  }

  efi_disk {
    datastore_id = var.storage
    file_format  = "raw"
    type         = "4m"
  }

  # Boot disk (NixOS root)
  disk {
    datastore_id = var.storage
    import_from  = module.wyrm2_image.import_path
    interface    = "scsi0"
    iothread     = true
    discard      = "on"
    size         = 500
  }

  # Data disks — all on NVMe (local-zfs) unless noted.
  # NixOS mounts these by PCI path (nix/nixos/hosts/wyrm2/default.nix); the
  # guest's /dev/vdX letters shift when slots are added or skipped.
  # Repurposed from the decommissioned Longhorn disk (was 100GB, /var/mnt/longhorn).
  # NOTE: the actual grow was applied imperatively via `qm` on atlas (see
  # nix/docs/wyrm2_display.md); this keeps the TF source in sync.
  # backup/replicate off — games are re-downloadable, not worth snapshotting.
  disk {
    datastore_id = var.storage
    interface    = "virtio1"
    iothread     = true
    discard      = "on"
    size         = 500
    backup       = false
    replicate    = false
    file_format  = "raw"
  } # Steam library (/games) — SSD, gaming seat
  disk {
    datastore_id = var.storage
    interface    = "virtio3"
    iothread     = true
    discard      = "on"
    size         = 200
    file_format  = "raw"
  } # containerd (/var/lib/containerd)
  # VM disk changes are ignored by the lifecycle below; apply this resize on Atlas.
  disk {
    datastore_id = var.storage
    interface    = "virtio4"
    iothread     = true
    discard      = "on"
    size         = 300
    backup       = false
    replicate    = false
    file_format  = "raw"
  } # Bazel caches — SSD (~/.cache/bazel)
  disk {
    datastore_id = "tank-hdd"
    interface    = "virtio7"
    iothread     = true
    discard      = "on"
    size         = 1024
    file_format  = "raw"
  } # /tmp scratch space (HDD)
  # Hotplugged imperatively via `qm set 110 --virtio9 ...` on atlas (tofu can't add disks
  # under the ignore_changes below); this keeps the TF source in sync.
  # backup/replicate off — the WAL only buffers metrics not yet shipped to Mimir.
  disk {
    datastore_id = "tank-hdd"
    interface    = "virtio9"
    iothread     = true
    discard      = "on"
    size         = 16
    backup       = false
    replicate    = false
    file_format  = "raw"
  } # alloy-node WAL (/var/lib/alloy-node) — HDD
  # Hotplugged imperatively via `qm set 110 --virtio10 ...` because Terraform
  # ignores disk changes while legacy CSI attachments remain. Keep this declaration
  # aligned with the attached volume; NixOS mounts it at /var/cache/agentydragon.
  # Rebuildable caches do not need Proxmox backups or replication.
  disk {
    datastore_id = "tank-hdd"
    interface    = "virtio10"
    iothread     = true
    discard      = "on"
    size         = 200
    backup       = false
    replicate    = false
    file_format  = "raw"
  } # Wyrm2 compiler and Python caches — HDD (sccache, uv)
  # Hotplugged imperatively because Terraform ignores disk changes while the
  # legacy CSI attachments remain. Keep this declaration aligned with the
  # attached volume; NixOS uses it for kubelet state, emptyDirs, and pod logs.
  # It is not replicated or backed up: pod-local data and logs are ephemeral.
  disk {
    datastore_id = "tank-hdd"
    interface    = "virtio11"
    iothread     = true
    discard      = "on"
    size         = 500
    backup       = false
    replicate    = false
    file_format  = "raw"
  } # Wyrm2 kubelet pod storage — HDD
  # Piraeus LVM_THIN pools, hotplugged imperatively like the disks above: sparse
  # zvols rpool/data/vm-110-piraeus-ssd (volblocksize 16K) and
  # tank/vm-110-piraeus-hdd (64K), both primarycache=metadata. Piraeus finds them
  # by serial (/dev/disk/by-id/virtio-piraeus-{ssd,hdd}). Proxmox backup and
  # replication stay off: volume backup belongs to the Piraeus layer.
  disk {
    datastore_id = var.storage
    interface    = "virtio12"
    iothread     = true
    discard      = "on"
    size         = 150
    serial       = "piraeus-ssd"
    backup       = false
    replicate    = false
    file_format  = "raw"
  } # Piraeus ssd pool
  disk {
    datastore_id = "tank-hdd"
    interface    = "virtio13"
    iothread     = true
    discard      = "on"
    size         = 500
    serial       = "piraeus-hdd"
    backup       = false
    replicate    = false
    file_format  = "raw"
  } # Piraeus hdd pool

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  started = true

  agent {
    enabled = true
    timeout = "2m"
  }

  # CLEANUP(added 2026-08-05): Remove once the legacy Proxmox CSI scsi1/scsi2
  # attachments are deliberately detached from VM 110 and a refreshed plan
  # reports only the declared scsi0 and virtio disks. Although the CSI
  # driver was removed 2026-07-16, those two unmounted disks remain attached;
  # allowing tofu to reconcile the incomplete disk state would remap live disks.
  lifecycle {
    ignore_changes = [disk]
  }

  depends_on = [module.wyrm2_image]
}

# NIXOS-REBUILD (optional — deploys full wyrm2 config from GitHub)

resource "null_resource" "wyrm2_nixos_rebuild" {
  count = var.nixos_rebuild ? 1 : 0

  triggers = {
    run = timestamp()
  }

  connection {
    type    = "ssh"
    host    = proxmox_virtual_environment_vm.wyrm2.ipv4_addresses[1][0]
    user    = "root"
    timeout = "5m"
    agent   = true
  }

  provisioner "remote-exec" {
    inline = [
      "until nix --version 2>/dev/null; do sleep 2; done",
      "nixos-rebuild switch --flake github:agentydragon/ducktape?ref=devel#wyrm2",
    ]
  }

  depends_on = [proxmox_virtual_environment_vm.wyrm2]
}
