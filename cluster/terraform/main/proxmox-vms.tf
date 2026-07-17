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

  cpu {
    cores = 32
    type  = "host"
  }

  memory {
    # 96GB. Reduced from 112GB — with balloon=0 (VFIO requires pinned memory),
    # 112GB + Talos CP (8GB) left only 8GB for host+ZFS ARC, causing ZFS write
    # stalls (memory_available_bytes went negative). 96GB leaves 24GB headroom.
    dedicated = 98304
    floating  = 0 # Disable balloon (VFIO incompatible)
  }

  # GPU passthrough. hostpci0 → guest 01:00.0 (first PCI), hostpci1 → guest
  # 02:00.0. The DISPLAY GPU must be guest 01:00.0 because DXVK renders on the
  # first-PCI device and the two 5090s are identical (no per-app selector) — so
  # the monitor's card has to be hostpci0 to avoid a cross-PCIe copy per frame.
  # See debug/atlas/direct_display_bringup/README.md.
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
  # ez Share WiFi SD card reader (MediaTek MT7961) — for CPAP data sync CronJob.
  # Passed through by vendor:product ID so it survives atlas USB port changes.
  # Requires a one-time wyrm2 restart to activate USB hotplug.
  usb {
    host = "0e8d:7961"
    usb3 = true
  }
  # RTL-SDR Blog V4 (Realtek RTL2838) — for SDR streaming via rtl_tcp.
  usb {
    host = "0bda:2838"
    usb3 = true
  }
  # TEX Shura keyboard via the FV43U monitor hub's USB-B uplink → atlas front
  # port (bus 3 port 12), passed by PORT PATH through the hub (QEMU can't pass
  # hubs themselves): grabbed only while the monitor's KVM routes its hub to
  # USB-B; on the USB-C/KVM side the path is empty and atlas keeps the
  # keyboard. Feeds logind seatphysical (direct-display gaming) — see
  # debug/atlas/direct_display_bringup/README.md. Update the path if the cable moves
  # to a rear port.
  usb {
    host = "3-12.1"
    usb3 = true
  }
  # Spare TEX Shura (second unit, identical 04d9:0532) on atlas front port
  # 3-1, permanently passed to wyrm2 as the seatphysical debug/test keyboard —
  # lets the direct-display iteration run without KVM-flipping the main
  # Shura away from atlas.
  usb {
    host = "3-1"
    usb3 = true
  }

  hotplug = "network,disk,cpu,usb" # note: memory hotplug requires NUMA

  # virtio-gl (VirGL): guest GL runs on atlas's iGPU, so Mutter composites
  # with GPU acceleration even when the NVIDIA GPUs are locked up or display-
  # less (plain virtio fell back to llvmpipe → choppy SPICE audio — see
  # debug/atlas/spice_audio/README.md).
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
  # virtio0=/dev/vda, virtio1=/dev/vdb, ..., virtio8=/dev/vdi
  disk {
    datastore_id = var.storage
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = 500
    file_format  = "raw"
  } # local-path provisioner (/var/local-path-provisioner)
  # Repurposed from the decommissioned Longhorn disk (was 100GB, /var/mnt/longhorn).
  # NOTE: the actual grow was applied imperatively via `qm` on atlas (see
  # debug/atlas/direct_display_bringup/README.md); this keeps the TF source in sync.
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
    interface    = "virtio2"
    iothread     = true
    discard      = "on"
    size         = 500
    file_format  = "raw"
  } # OpenEBS LVM SSD (VG openebs-proxmox-ssd)
  disk {
    datastore_id = var.storage
    interface    = "virtio3"
    iothread     = true
    discard      = "on"
    size         = 200
    file_format  = "raw"
  } # containerd (/var/lib/containerd)
  disk {
    datastore_id = var.storage
    interface    = "virtio4"
    iothread     = true
    discard      = "on"
    size         = 150
    file_format  = "raw"
  } # Bazel output base — SSD (~/.cache/bazel)
  disk {
    datastore_id = "tank-hdd"
    interface    = "virtio5"
    iothread     = true
    discard      = "on"
    size         = 100
    file_format  = "raw"
  } # Bazel repository cache — HDD (~/.cache/bazel/.../cache/repos)
  disk {
    datastore_id = "tank-hdd"
    interface    = "virtio6"
    iothread     = true
    discard      = "on"
    size         = 500
    file_format  = "raw"
  } # OpenEBS LVM HDD (VG openebs-proxmox-hdd)
  disk {
    datastore_id = "tank-hdd"
    interface    = "virtio7"
    iothread     = true
    discard      = "on"
    size         = 1024
    file_format  = "raw"
  } # /tmp scratch space (HDD)
  disk {
    datastore_id = var.storage
    interface    = "virtio8"
    iothread     = true
    discard      = "on"
    size         = 500
    backup       = false
    replicate    = false
    file_format  = "raw"
  } # Colibri disk-streamed model storage (/var/lib/colibri) — SSD

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  started = true

  agent {
    enabled = true
    timeout = "2m"
  }

  # No `ignore_changes = [disk]`: the Proxmox CSI driver that used to hotplug
  # unmanaged scsi disks onto this VM was removed 2026-07-16
  # (see cluster/docs/lessons_learned/2026_07_16_disable_proxmox_csi.md), so the
  # `disk` blocks above are now the full, authoritative disk shape. tofu manages
  # all wyrm2 disks declaratively again.

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
