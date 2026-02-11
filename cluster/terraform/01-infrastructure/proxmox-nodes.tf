# Proxmox Home Nodes
# 1x controlplane (talos-pve-cp-0) + 1x worker (talos-pve-worker-0) on home Proxmox (atlas)
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

# ============================================================================
# PROXMOX DISK IMAGE
# ============================================================================

# Download shared disk image - one image for all nodes (network via cloud-init)
resource "proxmox_virtual_environment_download_file" "talos_disk" {
  content_type = "import"
  datastore_id = "local" # dir storage, configured via ansible for images content
  node_name    = var.proxmox_node_name
  # Replace any .raw.xz or .raw.zst extension with .qcow2 for Proxmox import
  url       = replace(replace(data.talos_image_factory_urls.proxmox.urls.disk_image, ".raw.xz", ".qcow2"), ".raw.zst", ".qcow2")
  file_name = "talos-${talos_image_factory_schematic.proxmox.id}-amd64.qcow2"
  overwrite = true
}

# ============================================================================
# CLOUD-INIT NETWORK SNIPPETS
# ============================================================================

# Create per-node network-config snippets for cloud-init
resource "proxmox_virtual_environment_file" "network_config" {
  for_each = local.proxmox_nodes

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
    cores = 4
  }

  memory {
    dedicated = 12 * 1024                                       # 12GB max
    floating  = each.value.type == "controlplane" ? 4096 : 6144 # 4GB controllers, 6GB workers
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

# ============================================================================
# TALOS MACHINE CONFIGURATION
# ============================================================================

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

  config_patches = concat([
    # Common configuration for all nodes
    yamlencode({
      machine = {
        network = {
          # KubeSpan config stays in machine.network (not deprecated in v1.12)
          kubespan = {
            enabled             = true
            allowDownPeerBypass = true
          }
        }
        nodeLabels = {
          "topology.kubernetes.io/region" = "proxmox"
          "topology.kubernetes.io/zone"   = "atlas"
        }
        features = {
          kubePrism = {
            enabled = true
            port    = 7445
          }
          hostDNS = {
            enabled              = true
            forwardKubeDNSToHost = true
          }
        }
        kubelet = {
          extraArgs = {
            provider-id = "proxmox://cluster/${each.value.vm_id}"
            # Allow TCP MTU probing sysctl for PowerDNS AXFR over Tailscale/KubeSpan
            # Required to handle MTU mismatch (WireGuard 1280 vs pod 1500)
            allowed-unsafe-sysctls = "net.ipv4.tcp_mtu_probing"
          }
          # No nodeIP.validSubnets - let kubelet auto-detect
          # KubeSpan IPv6 IPs (fd05::/64) are globally routable via WireGuard mesh
        }
      }
      cluster = {
        # Allow scheduling on controlplanes for consistency with VPS nodes
        allowSchedulingOnControlPlanes = true
        discovery = {
          enabled = true
        }
        network = {
          cni = { name = "none" }
        }
        proxy = { disabled = true }
      }
    }),
  ],
    # Talos v1.12: disable DHCP on eth0 for workers via LinkConfig
    # (replaces deprecated machine.network.interfaces)
    # Applying any LinkConfig implicitly disables default DHCP on that interface
    each.value.type == "worker" ? [yamlencode({
      apiVersion = "v1alpha1"
      kind       = "LinkConfig"
      name       = "eth0"
      up         = true
    })] : [],
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
