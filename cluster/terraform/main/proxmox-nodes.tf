# Proxmox Home Nodes
# 1x controlplane (talos-pve-cp-0) on home Proxmox (atlas), 24 GB RAM
# Uses Nebula mesh for networking with VPS nodes

# ============================================================================
# TALOS IMAGE FACTORY - Generate custom Talos image with extensions
# ============================================================================

# Shared schematic with just extensions (network config via cloud-init snippets)
resource "talos_image_factory_schematic" "proxmox" {
  schematic = yamlencode({
    customization = {
      extraKernelArgs = ["net.ifnames=0", "console=ttyS0,115200"]
      systemExtensions = {
        officialExtensions = [
          "siderolabs/qemu-guest-agent",
          "siderolabs/iscsi-tools",
          "siderolabs/nebula",
        ]
      }
    }
  })
}

# Get download URL for shared schematic
data "talos_image_factory_urls" "proxmox" {
  schematic_id  = talos_image_factory_schematic.proxmox.id
  talos_version = var.proxmox_talos_version
  platform      = "nocloud" # nocloud platform reads cloud-init from cidata ISO
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

# ============================================================================
# CLOUD-INIT NETWORK SNIPPETS
# ============================================================================

# Create per-node network-config snippets for cloud-init (all Proxmox nodes)
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
    dedicated = 8 * 1024 # 8GB (pure control plane, no workloads)
    floating  = 0        # Disable balloon — OOM kills kube-apiserver when ballooned down
  }

  serial_device {}

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

# ============================================================================
# TALOS MACHINE CONFIGURATION
# ============================================================================

# Common config patch builder for all Proxmox nodes
locals {
  # Base config patch shared by all Proxmox nodes
  proxmox_base_config_patch = {
    machine = local.common_machine_base
    cluster = local.common_cluster_config
  }

  # Worker LinkConfig: disable DHCP on eth0
  worker_link_config = yamlencode({
    apiVersion = "v1alpha1"
    kind       = "LinkConfig"
    name       = "eth0"
    up         = true
  })

  # Common node labels for all Proxmox nodes
  proxmox_node_labels = {
    "topology.kubernetes.io/region"                   = "proxmox"
    "topology.kubernetes.io/zone"                     = "atlas"
    "csi.proxmox.sinextra.dev/max-volume-attachments" = "29"
  }

  # AuthenticationConfiguration file for Proxmox CP nodes.
  # Written to host filesystem; mounted into kube-apiserver via extraVolumes.
  # CP nodes only — workers don't run kube-apiserver.
  # permissions = 416 (= 0640 octal: owner rw, group r, world none)
  proxmox_cp_auth_files = [
    {
      content     = local.auth_config_content
      path        = "/etc/kubernetes/auth/auth-config.yaml"
      op          = "overwrite"
      permissions = 416
    }
  ]

}

data "talos_machine_configuration" "proxmox" {
  for_each = local.proxmox_nodes

  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  machine_secrets    = local.machine_secrets
  machine_type       = each.value.type
  talos_version      = var.proxmox_talos_version
  kubernetes_version = var.kubernetes_version
  examples           = false
  docs               = false

  config_patches = concat(
    [yamlencode(merge(local.proxmox_base_config_patch, {
      machine = merge(local.proxmox_base_config_patch.machine, {
        # Explicit network config — cloud-init network-config isn't preserved
        # across talosctl upgrade (kexec or powercycle). Without this, the node
        # gets a DHCP address instead of the configured static IP.
        network = merge(local.proxmox_base_config_patch.machine.network, {
          interfaces = [{
            interface = "eth0"
            dhcp      = false
            addresses = ["${each.value.ip}/16"]
            routes = [{
              network = "0.0.0.0/0"
              gateway = local.proxmox_gateway
            }]
          }]
          nameservers = ["1.1.1.1", "8.8.8.8"]
        })
        nodeLabels = local.proxmox_node_labels
        kubelet = merge(local.common_machine_base.kubelet, {
          extraArgs = {
            provider-id            = "proxmox://cluster/${each.value.vm_id}"
            allowed-unsafe-sysctls = "net.ipv4.tcp_mtu_probing"
          }
        })
        # Write AuthenticationConfiguration for CP nodes only (workers don't run kube-apiserver).
        files = each.value.type == "controlplane" ? local.proxmox_cp_auth_files : []
      })
      })),
      # Explicit hostname — overrides HostnameConfig auto: stable (Talos v1.12+).
      # Also needed because `talosctl upgrade` uses kexec which doesn't re-read
      # nocloud platform metadata (cloud-init). Without this, the node loses BOTH
      # its hostname AND static IP after upgrade (gets DHCP address instead of
      # the configured static IP, breaking etcd peering).
      # Use `talosctl upgrade --reboot-mode powercycle` as a workaround if this
      # patch isn't applied before upgrade.
      yamlencode({
        apiVersion = "v1alpha1"
        kind       = "HostnameConfig"
        auto       = "off"
        hostname   = each.value.name
      }),
    ],
    each.value.type == "worker" ? [local.worker_link_config] : [],
    local.nebula_machine_patches[each.key],
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
