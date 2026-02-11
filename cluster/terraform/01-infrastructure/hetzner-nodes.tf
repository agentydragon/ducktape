# Hetzner VPS Nodes
# 2x CPX31 controlplane+worker nodes in Hillsboro, OR
#
# Talos is pre-installed on a Hetzner snapshot (built by Packer via rescue+dd).
# Servers boot directly from the snapshot — single Talos boot, single identity.
# This eliminates the KubeSpan phantom peer problem from ISO-to-disk reboot.

# ============================================================================
# TALOS IMAGE FACTORY - Generate custom Talos image for Hetzner
# ============================================================================

resource "talos_image_factory_schematic" "hcloud" {
  schematic = yamlencode({
    customization = {
      systemExtensions = {
        officialExtensions = [
          "siderolabs/qemu-guest-agent"
        ]
      }
    }
  })
}

data "talos_image_factory_urls" "hcloud" {
  schematic_id  = talos_image_factory_schematic.hcloud.id
  talos_version = var.talos_version
  platform      = "hcloud"
  architecture  = "amd64"
}

# ============================================================================
# HETZNER SNAPSHOT - Built by Packer (rescue mode + dd)
# ============================================================================

resource "terraform_data" "talos_hcloud_image" {
  triggers_replace = [
    var.talos_version,
    talos_image_factory_schematic.hcloud.id,
  ]

  provisioner "local-exec" {
    working_dir = "${path.module}/packer"
    command     = <<-EOT
      set -e
      SELECTOR="os=talos,version=${var.talos_version},schematic_id=${substr(talos_image_factory_schematic.hcloud.id, 0, 8)}"
      if hcloud image list --type snapshot --selector "$SELECTOR" -o noheader 2>/dev/null | grep -q .; then
        echo "Snapshot already exists (selector: $SELECTOR), skipping Packer build"
        exit 0
      fi
      packer init talos-hcloud.pkr.hcl && \
      packer build \
        -var 'talos_image_url=${data.talos_image_factory_urls.hcloud.urls.disk_image}' \
        -var 'talos_version=${var.talos_version}' \
        -var 'schematic_id=${talos_image_factory_schematic.hcloud.id}' \
        -var 'server_location=${var.hetzner_location}' \
        talos-hcloud.pkr.hcl
    EOT
    environment = {
      HCLOUD_TOKEN = nonsensitive(var.hcloud_token)
    }
  }
}

data "hcloud_image" "talos" {
  with_selector     = "os=talos,version=${var.talos_version},schematic_id=${substr(talos_image_factory_schematic.hcloud.id, 0, 8)}"
  most_recent       = true
  with_architecture = "x86"

  depends_on = [terraform_data.talos_hcloud_image]
}

# ============================================================================
# VPS SERVERS
# ============================================================================

resource "hcloud_server" "vps" {
  for_each = local.vps_nodes

  name        = each.value.name
  server_type = each.value.server_type
  location    = var.hetzner_location
  image       = data.hcloud_image.talos.id
  ssh_keys    = [hcloud_ssh_key.talos.id]
  user_data   = data.talos_machine_configuration.vps[each.key].machine_configuration

  labels = {
    cluster = var.cluster_name
    role    = "controlplane"
    node    = each.key
  }

  backups = true

  firewall_ids = [hcloud_firewall.talos.id]

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }
}

# ============================================================================
# TALOS MACHINE CONFIGURATION
# ============================================================================

data "talos_machine_configuration" "vps" {
  for_each = local.vps_nodes

  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  machine_secrets    = local.machine_secrets
  machine_type       = "controlplane"
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
  examples           = false
  docs               = false

  config_patches = [
    yamlencode({
      machine = {
        network = {
          hostname = each.value.name
          # Talos HostDNS writes 169.254.116.108 (its own link-local listener) into
          # resolv.conf when forwardKubeDNSToHost is enabled. Use public DNS to
          # ensure containerd and host-level resolution don't depend on it.
          nameservers = ["1.1.1.1", "8.8.8.8"]
          kubespan = {
            enabled             = true
            allowDownPeerBypass = true
          }
        }
        nodeLabels = {
          "topology.kubernetes.io/region" = "hetzner"
          "topology.kubernetes.io/zone"   = var.hetzner_location
        }
        features = {
          kubePrism = {
            enabled = true
            port    = 7445
          }
          hostDNS = {
            enabled = true
            # forwardKubeDNSToHost breaks DNS resolution on Hetzner VPS with
            # Cilium VXLAN: containerd image pulls fail with "server misbehaving"
            # from 127.0.0.53 even after patching nameservers to public DNS.
            # Disabling lets pods use CoreDNS directly (which forwards to 1.1.1.1/8.8.8.8).
            forwardKubeDNSToHost = false
          }
        }
        kubelet = {
          # Allow TCP MTU probing sysctl for PowerDNS AXFR over Tailscale/KubeSpan
          # Required to handle MTU mismatch (WireGuard 1280 vs pod 1500)
          extraArgs = {
            allowed-unsafe-sysctls = "net.ipv4.tcp_mtu_probing"
          }
        }
      }
      cluster = {
        # Each VPS controlplane node consumes a whole VPS instance, so we need
        # to allow scheduling workloads on them to utilize the VPS resources
        allowSchedulingOnControlPlanes = true
        discovery = {
          enabled = true
        }
        network = {
          cni = { name = "none" }
        }
        proxy = { disabled = true }
      }
    })
  ]
}

# ============================================================================
# MACHINE CONFIGURATION APPLY
# ============================================================================

resource "talos_machine_configuration_apply" "vps" {
  for_each = local.vps_nodes

  client_configuration        = local.client_configuration
  machine_configuration_input = data.talos_machine_configuration.vps[each.key].machine_configuration
  node                        = hcloud_server.vps[each.key].ipv4_address

  depends_on = [hcloud_server.vps]
}
