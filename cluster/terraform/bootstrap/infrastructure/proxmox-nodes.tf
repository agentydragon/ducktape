# Proxmox Home Nodes
# 1x controlplane (talos-pve-cp-0) on home Proxmox (atlas), 24 GB RAM
# 1x GPU worker (talos-pve-gpu-worker-0) with 2x RTX 5090, 32 GB fixed RAM
# Uses KubeSpan for mesh networking with VPS nodes

# ============================================================================
# TALOS IMAGE FACTORY - Generate custom Talos image with extensions
# ============================================================================

# Shared schematic with just extensions (network config via cloud-init snippets)
resource "talos_image_factory_schematic" "proxmox" {
  schematic = yamlencode({
    customization = {
      extraKernelArgs = ["net.ifnames=0"]
      systemExtensions = {
        officialExtensions = [
          "siderolabs/qemu-guest-agent"
        ]
      }
    }
  })
}

# Get download URL for shared schematic
data "talos_image_factory_urls" "proxmox" {
  schematic_id  = talos_image_factory_schematic.proxmox.id
  talos_version = var.talos_version
  platform      = "nocloud" # nocloud platform reads cloud-init from cidata ISO
  architecture  = "amd64"
}

# GPU schematic with NVIDIA extensions for GPU worker nodes
resource "talos_image_factory_schematic" "proxmox_gpu" {
  schematic = yamlencode({
    customization = {
      extraKernelArgs = ["net.ifnames=0"]
      systemExtensions = {
        officialExtensions = [
          "siderolabs/qemu-guest-agent",
          "siderolabs/nvidia-open-gpu-kernel-modules",
          "siderolabs/nvidia-container-toolkit",
        ]
      }
    }
  })
}

data "talos_image_factory_urls" "proxmox_gpu" {
  schematic_id  = talos_image_factory_schematic.proxmox_gpu.id
  talos_version = var.talos_version
  platform      = "nocloud"
  architecture  = "amd64"
}

# ============================================================================
# PROXMOX DISK IMAGES
# ============================================================================

# Standard disk image for non-GPU nodes
resource "proxmox_virtual_environment_download_file" "talos_disk" {
  content_type = "import"
  datastore_id = "local" # dir storage, configured via ansible for images content
  node_name    = var.proxmox_node_name
  # Replace any .raw.xz or .raw.zst extension with .qcow2 for Proxmox import
  url       = replace(replace(data.talos_image_factory_urls.proxmox.urls.disk_image, ".raw.xz", ".qcow2"), ".raw.zst", ".qcow2")
  file_name = "talos-${talos_image_factory_schematic.proxmox.id}-amd64.qcow2"
  overwrite = true
}

# GPU disk image with NVIDIA extensions
resource "proxmox_virtual_environment_download_file" "talos_disk_gpu" {
  content_type = "import"
  datastore_id = "local"
  node_name    = var.proxmox_node_name
  url          = replace(replace(data.talos_image_factory_urls.proxmox_gpu.urls.disk_image, ".raw.xz", ".qcow2"), ".raw.zst", ".qcow2")
  file_name    = "talos-gpu-${talos_image_factory_schematic.proxmox_gpu.id}-amd64.qcow2"
  overwrite    = true
}

# ============================================================================
# GPU PCI HARDWARE MAPPINGS
# ============================================================================

resource "proxmox_virtual_environment_hardware_mapping_pci" "gpu0" {
  name    = "gpu0"
  comment = "NVIDIA RTX 5090 #0 (ZOTAC)"
  map = [
    {
      id           = "10de:2b85"
      iommu_group  = 14
      node         = var.proxmox_node_name
      path         = "0000:01:00.0"
      subsystem_id = "19da:1761"
    },
  ]
}

resource "proxmox_virtual_environment_hardware_mapping_pci" "gpu1" {
  name    = "gpu1"
  comment = "NVIDIA RTX 5090 #1 (Gigabyte)"
  map = [
    {
      id           = "10de:2b85"
      iommu_group  = 16
      node         = var.proxmox_node_name
      path         = "0000:03:00.0"
      subsystem_id = "1458:416f"
    },
  ]
}

# ============================================================================
# CLOUD-INIT NETWORK SNIPPETS
# ============================================================================

# Create per-node network-config snippets for cloud-init (all Proxmox nodes)
resource "proxmox_virtual_environment_file" "network_config" {
  for_each = merge(local.proxmox_nodes, local.proxmox_gpu_nodes)

  content_type = "snippets"
  datastore_id = "local"
  node_name    = var.proxmox_node_name

  source_raw {
    data = yamlencode({
      network = {
        version = 2
        ethernets = {
          eth0 = {
            dhcp4     = false
            dhcp6     = false
            addresses = ["${each.value.ip}/16"]
            gateway4  = local.proxmox_gateway
            nameservers = {
              addresses = ["1.1.1.1", "8.8.8.8"]
            }
          }
        }
      }
    })
    file_name = "talos-${each.key}-network.yaml"
  }
}

# ============================================================================
# PROXMOX VMS
# ============================================================================

resource "proxmox_virtual_environment_vm" "talos" {
  for_each = local.proxmox_nodes

  name            = each.value.name
  vm_id           = each.value.vm_id
  node_name       = var.proxmox_node_name
  tags            = sort(["talos", each.value.type, "kubernetes", "terraform", "hybrid"])
  stop_on_destroy = true
  bios            = "ovmf"
  machine         = "q35"
  scsi_hardware   = "virtio-scsi-single"

  operating_system {
    type = "l26"
  }

  cpu {
    type  = "host"
    cores = 16
  }

  memory {
    dedicated = 24 * 1024 # 24GB (consolidated from cp + worker)
    floating  = 12 * 1024 # 12GB minimum
  }

  vga {
    type = "qxl"
  }

  network_device {
    bridge = "vmbr4"
  }

  efi_disk {
    datastore_id = "local-zfs"
    file_format  = "raw"
    type         = "4m"
  }

  disk {
    datastore_id = "local-zfs"
    interface    = "scsi0"
    iothread     = true
    ssd          = true
    discard      = "on"
    size         = 300
    file_format  = "raw"
    import_from  = proxmox_virtual_environment_download_file.talos_disk.id
  }

  agent {
    enabled = true
    trim    = true
  }

  # Cloud-init drive for network configuration
  initialization {
    datastore_id         = "local-zfs"
    network_data_file_id = proxmox_virtual_environment_file.network_config[each.key].id
  }
}

# GPU worker VMs with PCIe passthrough
resource "proxmox_virtual_environment_vm" "talos_gpu" {
  for_each = local.proxmox_gpu_nodes

  name            = each.value.name
  vm_id           = each.value.vm_id
  node_name       = var.proxmox_node_name
  tags            = sort(["talos", each.value.type, "kubernetes", "terraform", "hybrid", "gpu"])
  stop_on_destroy = true
  bios            = "ovmf"
  machine         = "q35"
  scsi_hardware   = "virtio-scsi-single"

  operating_system {
    type = "l26"
  }

  cpu {
    type  = "host"
    cores = 8
  }

  memory {
    dedicated = 32 * 1024 # 32GB fixed — balloon incompatible with VFIO passthrough
    floating  = 0         # Disable balloon (QEMU inhibits it with VFIO devices)
  }

  vga {
    type = "qxl"
  }

  network_device {
    bridge = "vmbr4"
  }

  efi_disk {
    datastore_id = "local-zfs"
    file_format  = "raw"
    type         = "4m"
  }

  disk {
    datastore_id = "local-zfs"
    interface    = "scsi0"
    iothread     = true
    ssd          = true
    discard      = "on"
    size         = 40
    file_format  = "raw"
    import_from  = proxmox_virtual_environment_download_file.talos_disk_gpu.id
  }

  agent {
    enabled = true
    trim    = true
  }

  # GPU 0: NVIDIA RTX 5090 (PCI 01:00, IOMMU group 14)
  hostpci {
    device  = "hostpci0"
    mapping = proxmox_virtual_environment_hardware_mapping_pci.gpu0.name
    pcie    = true
    rombar  = true
  }

  # GPU 1: NVIDIA RTX 5090 (PCI 03:00, IOMMU group 16)
  hostpci {
    device  = "hostpci1"
    mapping = proxmox_virtual_environment_hardware_mapping_pci.gpu1.name
    pcie    = true
    rombar  = true
  }

  # Cloud-init drive for network configuration
  initialization {
    datastore_id         = "local-zfs"
    network_data_file_id = proxmox_virtual_environment_file.network_config[each.key].id
  }
}

# ============================================================================
# TALOS MACHINE CONFIGURATION
# ============================================================================

# Common config patch builder for all Proxmox nodes
locals {
  # Base config patch shared by all Proxmox nodes
  proxmox_base_config_patch = {
    machine = {
      network = {
        kubespan = {
          enabled             = true
          allowDownPeerBypass = true
        }
      }
      features = {
        kubePrism = {
          enabled = true
          port    = 7445
        }
      }
      registries = {
        mirrors = local.registry_mirrors
      }
    }
    cluster = {
      allowSchedulingOnControlPlanes = true
      apiServer                      = { certSANs = ["api.${var.cluster_domain}"] }
      discovery                      = { enabled = true }
      network                        = { cni = { name = "none" } }
      proxy                          = { disabled = true }
    }
  }

  # Worker LinkConfig: disable DHCP on eth0
  worker_link_config = yamlencode({
    apiVersion = "v1alpha1"
    kind       = "LinkConfig"
    name       = "eth0"
    up         = true
  })

  # NVIDIA-specific config patches for GPU nodes
  nvidia_config_patches = [
    yamlencode({
      machine = {
        kernel = {
          modules = [
            { name = "nvidia" },
            { name = "nvidia_uvm" },
            { name = "nvidia_drm" },
            { name = "nvidia_modeset" },
          ]
        }
        sysctls = {
          "net.core.bpf_jit_harden" = "1"
        }
        files = [
          {
            path        = "/etc/cri/conf.d/20-customization.part"
            op          = "create"
            content     = <<-EOT
              [plugins]
                [plugins."io.containerd.cri.v1.runtime"]
                  [plugins."io.containerd.cri.v1.runtime".containerd]
                    default_runtime_name = "nvidia"
            EOT
            permissions = 0
          }
        ]
      }
    }),
  ]
}

data "talos_machine_configuration" "proxmox" {
  for_each = local.proxmox_nodes

  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  machine_secrets    = local.machine_secrets
  machine_type       = each.value.type
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
  examples           = false
  docs               = false

  config_patches = concat(
    [yamlencode(merge(local.proxmox_base_config_patch, {
      machine = merge(local.proxmox_base_config_patch.machine, {
        nodeLabels = {
          "topology.kubernetes.io/region" = "proxmox"
          "topology.kubernetes.io/zone"   = "atlas"
        }
        kubelet = {
          extraArgs = {
            provider-id            = "proxmox://cluster/${each.value.vm_id}"
            allowed-unsafe-sysctls = "net.ipv4.tcp_mtu_probing"
          }
        }
      })
    }))],
    each.value.type == "worker" ? [local.worker_link_config] : [],
  )
}

data "talos_machine_configuration" "proxmox_gpu" {
  for_each = local.proxmox_gpu_nodes

  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  machine_secrets    = local.machine_secrets
  machine_type       = each.value.type
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
  examples           = false
  docs               = false

  config_patches = concat(
    [yamlencode(merge(local.proxmox_base_config_patch, {
      machine = merge(local.proxmox_base_config_patch.machine, {
        nodeLabels = {
          "topology.kubernetes.io/region" = "proxmox"
          "topology.kubernetes.io/zone"   = "atlas"
          "nvidia.com/gpu"                = "true"
        }
        kubelet = {
          extraArgs = {
            provider-id            = "proxmox://cluster/${each.value.vm_id}"
            allowed-unsafe-sysctls = "net.ipv4.tcp_mtu_probing"
            register-with-taints   = "nvidia.com/gpu=true:PreferNoSchedule"
          }
        }
      })
    }))],
    local.nvidia_config_patches,
    [local.worker_link_config], # GPU nodes are always workers
  )
}

# ============================================================================
# MACHINE CONFIGURATION APPLY
# ============================================================================

resource "talos_machine_configuration_apply" "proxmox" {
  for_each = local.proxmox_nodes

  client_configuration        = local.client_configuration
  machine_configuration_input = data.talos_machine_configuration.proxmox[each.key].machine_configuration
  node                        = each.value.ip

  depends_on = [proxmox_virtual_environment_vm.talos]
}

resource "talos_machine_configuration_apply" "proxmox_gpu" {
  for_each = local.proxmox_gpu_nodes

  client_configuration        = local.client_configuration
  machine_configuration_input = data.talos_machine_configuration.proxmox_gpu[each.key].machine_configuration
  node                        = each.value.ip

  depends_on = [proxmox_virtual_environment_vm.talos_gpu]
}
