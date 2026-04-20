# Wyrm2 — NixOS dev workstation + k8s GPU worker on Proxmox

locals {
  repo_root = "${path.module}/../../.."
}

# ============================================================================
# NIXOS BOOTSTRAP IMAGE
# ============================================================================

# Bootstrap NixOS qcow2 image — minimal SSH-able image for initial VM provisioning.
# Only rebuilt when rebuild_image=true. After boot, nixos-rebuild deploys the full config.
module "wyrm2_image" {
  source        = "../../../terraform/modules/nixos-image"
  flake_target  = "bootstrap"
  proxmox_host  = var.proxmox_node_name
  repo_root     = local.repo_root
  build_enabled = var.rebuild_image
}

# ============================================================================
# VM INSTANCE
# ============================================================================

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

  # GPU passthrough
  hostpci {
    device = "hostpci0"
    id     = "0000:01:00.0"
    pcie   = true
    rombar = true
  }
  hostpci {
    device = "hostpci1"
    id     = "0000:03:00.0"
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

  hotplug = "network,disk,cpu,usb" # note: memory hotplug requires NUMA

  vga {
    type   = "virtio"
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
    size         = 300
  }

  # Data disks — all on NVMe (local-zfs) unless noted.
  # virtio0=/dev/vda, virtio1=/dev/vdb, ..., virtio5=/dev/vdf
  disk {
    datastore_id = var.storage
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = 500
    file_format  = "raw"
  } # local-path provisioner (/var/local-path-provisioner)
  disk {
    datastore_id = var.storage
    interface    = "virtio1"
    iothread     = true
    discard      = "on"
    size         = 100
    file_format  = "raw"
  } # Longhorn (/var/mnt/longhorn)
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
    size         = 40
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

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  started = true

  agent {
    enabled = true
    timeout = "2m"
  }

  lifecycle {
    ignore_changes = [
      # Proxmox CSI hotplugs scsi disks — provider can't distinguish
      # tofu-managed from CSI disks (TypeSet, no stable keys).
      disk,
    ]
  }

  depends_on = [module.wyrm2_image]
}

# ============================================================================
# NIXOS-REBUILD (optional — deploys full wyrm2 config from GitHub)
# ============================================================================

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
