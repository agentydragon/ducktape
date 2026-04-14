# Hetzner VPS Nodes
# 2x CPX31 controlplane nodes + 2x CPX31 worker nodes in Hillsboro, OR
#
# Talos is pre-installed on a Hetzner snapshot (built by Packer via rescue+dd).
# Servers boot directly from the snapshot — single Talos boot, single identity.
# This eliminates the dual-identity problem from ISO-to-disk reboot.

# ============================================================================
# TALOS IMAGE FACTORY - Generate custom Talos image for Hetzner
# ============================================================================

resource "talos_image_factory_schematic" "hcloud" {
  schematic = yamlencode({
    customization = {
      extraKernelArgs = [
        "talos.platform=hcloud",
      ]
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
  user_data   = local.vps_user_data[each.key]

  labels = {
    cluster = var.cluster_name
    role    = each.value.role
    node    = each.key
  }

  backups = true

  firewall_ids = [hcloud_firewall.talos.id]

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  # Hetzner treats user_data, image, and ssh_keys as immutable — any change
  # forces server replacement. Talos only reads user_data on first boot; ongoing
  # config is managed via the Talos API (talos_machine_configuration_apply).
  # Image changes are applied via talosctl upgrade, not server replacement.
  # ssh_keys must be ignored to prevent forced replacement on import/state drift.
  lifecycle {
    ignore_changes = [user_data, image, ssh_keys]
  }
}

# ============================================================================
# TALOS MACHINE CONFIGURATION
# ============================================================================

# VPS config patches — separate for CP and worker roles.
# Workers must not include etcd, apiServer, or kubernetesTalosAPIAccess
# (Talos rejects these on worker nodes).
locals {
  # Shared VPS machine settings (labels, install image, kubelet args)
  vps_machine_overrides = {
    # topology.kubernetes.io/region and zone are set by Talos CCM from
    # Hetzner platform metadata (region=hil, zone=hil-dc1). Do not set
    # them here — kubelet --node-labels would override the CCM values.
    nodeLabels = {
      "node.longhorn.io/create-default-disk" = "true"
    }
    install = {
      image = "factory.talos.dev/installer/${talos_image_factory_schematic.hcloud.id}:${var.talos_version}"
    }
  }

  vps_config_patch = yamlencode({
    machine = merge(local.common_machine_base, local.vps_machine_overrides, {
      kubelet = merge(local.common_machine_base.kubelet, {
        extraArgs = {
          allowed-unsafe-sysctls = "net.ipv4.tcp_mtu_probing"
          cloud-provider         = "external"
        }
      })
      # Write AuthenticationConfiguration to the host filesystem so kube-apiserver
      # can read it via the extraVolumes mount. CP nodes only — workers don't run kube-apiserver.
      # permissions = 416 (= 0640 octal: owner rw, group r, world none)
      files = [
        {
          content     = local.auth_config_content
          path        = "/etc/kubernetes/auth/auth-config.yaml"
          op          = "overwrite"
          permissions = 416
        }
      ]
    })
    cluster = local.common_cluster_config
  })

  vps_worker_config_patch = yamlencode({
    machine = merge(local.worker_machine_base, local.vps_machine_overrides, {
      kubelet = merge(local.worker_machine_base.kubelet, {
        extraArgs = {
          allowed-unsafe-sysctls = "net.ipv4.tcp_mtu_probing"
          cloud-provider         = "external"
        }
      })
    })
    cluster = local.worker_cluster_config
  })
}

# --- Controlplane machine configs ---

# Base config for hcloud_server.user_data — no nebula patches (would create a
# dependency cycle: hcloud_server.user_data → nebula_patch → hcloud_server.ipv4_address).
data "talos_machine_configuration" "vps" {
  for_each = local.vps_cp_nodes

  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  machine_secrets    = local.machine_secrets
  machine_type       = "controlplane"
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
  examples           = false
  docs               = false

  config_patches = [local.vps_config_patch]
}

# Full config with nebula + hostname — applied via Talos API after servers exist.
# Separate data source breaks the dependency cycle (nebula patches reference server IPs).
data "talos_machine_configuration" "vps_nebula" {
  for_each = local.vps_cp_nodes

  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  machine_secrets    = local.machine_secrets
  machine_type       = "controlplane"
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
  examples           = false
  docs               = false

  config_patches = concat(
    [
      local.vps_config_patch,
      # Explicit hostname — overrides the auto-generated HostnameConfig
      # (auto: stable) that the Terraform provider appends. Without this,
      # talosctl upgrade (kexec) loses the platform-derived hostname.
      yamlencode({
        apiVersion = "v1alpha1"
        kind       = "HostnameConfig"
        auto       = "off"
        hostname   = each.value.name
      }),
    ],
    local.nebula_machine_patches[each.key],
  )
}

# --- Worker machine configs ---

data "talos_machine_configuration" "vps_worker" {
  for_each = local.vps_worker_nodes

  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  machine_secrets    = local.machine_secrets
  machine_type       = "worker"
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
  examples           = false
  docs               = false

  config_patches = [local.vps_worker_config_patch]
}

data "talos_machine_configuration" "vps_worker_nebula" {
  for_each = local.vps_worker_nodes

  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  machine_secrets    = local.machine_secrets
  machine_type       = "worker"
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
  examples           = false
  docs               = false

  config_patches = concat(
    [
      local.vps_worker_config_patch,
      yamlencode({
        apiVersion = "v1alpha1"
        kind       = "HostnameConfig"
        auto       = "off"
        hostname   = each.value.name
      }),
    ],
    local.nebula_machine_patches[each.key],
  )
}

# --- Merged config maps for role-agnostic server/apply resources ---

locals {
  vps_user_data = merge(
    { for k in keys(local.vps_cp_nodes) : k => data.talos_machine_configuration.vps[k].machine_configuration },
    { for k in keys(local.vps_worker_nodes) : k => data.talos_machine_configuration.vps_worker[k].machine_configuration },
  )
  vps_machine_configs = merge(
    { for k in keys(local.vps_cp_nodes) : k => data.talos_machine_configuration.vps_nebula[k].machine_configuration },
    { for k in keys(local.vps_worker_nodes) : k => data.talos_machine_configuration.vps_worker_nebula[k].machine_configuration },
  )
}

# ============================================================================
# MACHINE CONFIGURATION APPLY
# ============================================================================

resource "talos_machine_configuration_apply" "vps" {
  for_each = local.vps_nodes

  client_configuration        = local.client_configuration
  machine_configuration_input = local.vps_machine_configs[each.key]
  node                        = hcloud_server.vps[each.key].ipv4_address

  depends_on = [hcloud_server.vps]
}
