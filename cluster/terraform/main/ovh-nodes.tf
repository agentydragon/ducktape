# OVH Eco Kimsufi bare metal nodes (KS-5 control plane + KS-GAME workers, HIL)
#
# Talos is installed via OVH rescue mode (netboot → dd image to disk).
# No Packer/snapshot mechanism available for OVH bare metal.
#
# Provisioning flow (per server):
#   1. Set rescue boot + reboot → rescue env (SSH with cluster SSH key)
#   2. dd Talos metal image to the node's configured install_disk
#   3. Set harddisk boot + reboot → Talos boots
#   4. Apply Talos machine config (includes Nebula extension)
#
# Prerequisites:
#   - Server purchased via OVH web UI; set the matching kimsufi_service_name var
#   - OVH API credentials in secrets/ovh-credentials.sops.yaml

# ============================================================================
# SERVER MAP
# ============================================================================

locals {
  kimsufi_servers = {
    kimsufi_worker0 = {
      service_name    = var.kimsufi_service_name
      hostname        = "ovh-ns103656"
      nebula_ip       = "10.42.0.13"
      role            = "controlplane"
      apply_mode      = "staged_if_needing_reboot"
      install_disk    = "/dev/sda"
      data_disk_match = "disk.dev_path == '/dev/sdb'"
      zone            = "hil-ovh"
    }
    kimsufi_worker1 = {
      service_name    = var.kimsufi_service_name_1
      hostname        = "ovh-ns103711"
      nebula_ip       = "10.42.0.14"
      role            = "controlplane"
      apply_mode      = "staged_if_needing_reboot"
      install_disk    = "/dev/sda"
      data_disk_match = "disk.dev_path == '/dev/sdb'"
      zone            = "hil-ovh"
    }
    ks_game_worker0 = {
      service_name    = var.kimsufi_service_name_ks_game_0
      hostname        = "ovh-ns104952"
      nebula_ip       = "10.42.0.16"
      role            = "worker"
      install_disk    = "/dev/disk/by-id/nvme-INTEL_SSDPE2MX450G7_BTPF8256006P450RGN"
      data_disk_match = "disk.serial == 'BTPF8304019P450RGN'"
      zone            = "hil-ovh"
    }
    ks_game_worker1 = {
      service_name    = var.kimsufi_service_name_ks_game_1
      hostname        = "ovh-ns104963"
      nebula_ip       = "10.42.0.17"
      role            = "worker"
      install_disk    = "/dev/disk/by-id/nvme-INTEL_SSDPE2MX450G7_BTPF8256002V450RGN"
      data_disk_match = "disk.serial == 'BTPF8256009U450RGN'"
      zone            = "hil-ovh"
    }
  }
  # Filter out unpurchased servers (empty service name)
  active_kimsufi_servers = {
    for k, v in local.kimsufi_servers : k => v if v.service_name != ""
  }

  kimsufi_cp_servers = {
    kimsufi_cp0 = {
      service_name    = var.kimsufi_service_name_cp0
      hostname        = "ovh-ns102453"
      nebula_ip       = "10.42.0.15"
      role            = "controlplane"
      install_disk    = "/dev/sda"
      data_disk_match = "disk.dev_path == '/dev/sdb'"
      zone            = "hil-ovh"
    }
  }
  active_kimsufi_cp_servers = {
    for k, v in local.kimsufi_cp_servers : k => v if v.service_name != ""
  }
}

# ============================================================================
# TALOS IMAGE FACTORY - Metal platform with Nebula extension
# ============================================================================

resource "talos_image_factory_schematic" "kimsufi" {
  schematic = yamlencode({
    customization = {
      # KS-5 has no physical display; we only see boot output via OVH IPMI SOL.
      # Without console=ttyS0 every Talos boot log is invisible — silent reboot
      # loops mask whether the kernel even started.
      extraKernelArgs = [
        "console=tty0",
        "console=ttyS0,115200n8",
      ]
      systemExtensions = {
        officialExtensions = [
          "siderolabs/nebula",
        ]
      }
    }
  })
}

data "talos_image_factory_urls" "kimsufi" {
  schematic_id  = talos_image_factory_schematic.kimsufi.id
  talos_version = var.talos_version
  platform      = "metal"
  architecture  = "amd64"
}

# ============================================================================
# OVH SERVER DATA SOURCES
# ============================================================================

data "ovh_dedicated_server" "kimsufi" {
  for_each     = local.active_kimsufi_servers
  service_name = each.value.service_name
}

data "ovh_dedicated_server" "kimsufi_cp" {
  for_each     = local.active_kimsufi_cp_servers
  service_name = each.value.service_name
}

# Boot IDs are hardcoded because data.ovh_dedicated_server_boots returns BOTH
# the customer rescue (rescue12-customer, Debian-12-based) AND the iPXE shell
# (ipxe-shell, an interactive bootloader that does NOT auto-launch a Linux),
# and the data source only returns the IDs — no kernel/description to filter by.
# Picking [0] from the list gives iPXE shell → SSH never comes up → install hangs.
# Verified via GET /dedicated/server/{name}/boot/{id}.
locals {
  kimsufi_rescue_boot_id   = 218949 # rescue12-customer (Debian-12)
  kimsufi_harddisk_boot_id = 1      # harddisk
}

# ============================================================================
# SSH KEY — for OVH rescue mode authentication
# ============================================================================

# ED25519 keypair injected into the OVH rescue environment so remote-exec can
# SSH in to dd the Talos image. Stored in SOPS (secrets/ovh-rescue-ssh.sops.yaml),
# not generated in-TF, so it survives state loss without breaking access.
data "sops_file" "ovh_rescue_ssh" {
  source_file = "${path.module}/../../../secrets/ovh-rescue-ssh.sops.yaml"
}

# ============================================================================
# OVH SERVER RESOURCES — sets rescue SSH key + EFI bootloader
# ============================================================================

resource "ovh_dedicated_server" "kimsufi" {
  for_each = local.active_kimsufi_servers

  service_name   = each.value.service_name
  rescue_ssh_key = data.sops_file.ovh_rescue_ssh.data["public_key"]
  # Match iam.displayName so the resource's Read→Update cycle doesn't try to
  # PUT /services/{id}. Even with the scope granted, the PUT hangs for ~10 min
  # then times out when the underlying server is in cancellation state
  # (OVH-side service marked for non-renewal). Keeping this set to the OVH
  # service name makes state == config, so no PUT is ever attempted.
  # See cluster/docs/lessons_learned/2026_05_13_provisioning_ovh_kimsufi.md §2.
  display_name = each.value.service_name
  # Without this, OVH's iPXE falls back to rEFInd which "starts" the Talos UKI
  # but doesn't actually run it — control returns to firmware, BIOS reboots,
  # forever. systemd-boot (dropped at this path by the Talos metal image) IS
  # UKI-aware and chainloads it properly.
  efi_bootloader_path = "\\efi\\boot\\bootx64.efi"
}

# ============================================================================
# TALOS INSTALLATION — rescue boot, dd image, harddisk reboot
# ============================================================================

# Step 1: Set rescue boot mode.
# ignore_changes on boot_id: after initial creation this sets boot to rescue.
# Step 4 (kimsufi_harddisk) overwrites it to harddisk. Without ignore_changes,
# subsequent plans would see drift and try to revert to rescue.
resource "ovh_dedicated_server_update" "kimsufi_rescue" {
  for_each     = local.active_kimsufi_servers
  service_name = each.value.service_name
  boot_id      = local.kimsufi_rescue_boot_id
  depends_on   = [ovh_dedicated_server.kimsufi]

  lifecycle {
    ignore_changes = [boot_id]
  }
}

# Step 2: Reboot into rescue.
resource "ovh_dedicated_server_reboot_task" "kimsufi_to_rescue" {
  for_each     = local.active_kimsufi_servers
  service_name = each.value.service_name
  keepers      = [tostring(local.kimsufi_rescue_boot_id)]
  depends_on   = [ovh_dedicated_server_update.kimsufi_rescue]
}

# Step 3: SSH into rescue, dd Talos image.
# connection.timeout covers the window waiting for rescue to boot over SSH.
# triggers on schematic_id so a kernel/extension change triggers re-provisioning.
# To re-provision: tofu taint null_resource.install_talos_kimsufi["kimsufi_worker0"]
resource "null_resource" "install_talos_kimsufi" {
  for_each = local.active_kimsufi_servers

  # Re-run when the schematic changes (which changes the install image URL).
  # Schematic changes mean kernel-args/extensions changed — we want a fresh dd.
  triggers = {
    schematic_id = talos_image_factory_schematic.kimsufi.id
  }

  connection {
    type        = "ssh"
    host        = data.ovh_dedicated_server.kimsufi[each.key].ip
    user        = "root"
    private_key = data.sops_file.ovh_rescue_ssh.data["private_key"]
    timeout     = "15m"
  }

  provisioner "remote-exec" {
    # OVH rescue runs dash (no `set -o pipefail`). Decompress to a temp file
    # and dd from that, so an unrelated decompressor failure can't silently
    # feed dd zero bytes. `test -s` makes sure we actually got a raw image.
    # The Image Factory currently ships `metal-amd64.raw.zst`; older releases
    # used .xz, hence the URL-suffix switch.
    # KS-5 has 32 GB RAM; /tmp on tmpfs has room for the ~1.5 GB raw image.
    # install_disk is per node: KS-5 uses /dev/sda, KS-GAME uses NVMe.
    inline = [
      "set -ex",
      # OVH Debian rescue doesn't have zstd pre-installed; xz-utils is there.
      "apt-get update -qq && apt-get install -y -qq zstd",
      "URL='${data.talos_image_factory_urls.kimsufi.urls.disk_image}'",
      "wget -q -O /tmp/talos.bin \"$URL\"",
      "case \"$URL\" in *.zst) zstd -dc /tmp/talos.bin > /tmp/talos.raw ;; *.xz) xz -dc /tmp/talos.bin > /tmp/talos.raw ;; *) echo \"unknown compression in $URL\" >&2; exit 1 ;; esac",
      "test -s /tmp/talos.raw",
      "dd if=/tmp/talos.raw of=${each.value.install_disk} bs=4M status=progress",
      "sync",
    ]
  }

  depends_on = [ovh_dedicated_server_reboot_task.kimsufi_to_rescue]
}

# Step 4: Switch to harddisk boot.
resource "ovh_dedicated_server_update" "kimsufi_harddisk" {
  for_each     = local.active_kimsufi_servers
  service_name = each.value.service_name
  boot_id      = local.kimsufi_harddisk_boot_id
  depends_on   = [null_resource.install_talos_kimsufi]
}

# Step 5: Reboot into Talos.
resource "ovh_dedicated_server_reboot_task" "kimsufi_to_talos" {
  for_each     = local.active_kimsufi_servers
  service_name = each.value.service_name
  keepers      = [tostring(local.kimsufi_harddisk_boot_id)]
  depends_on   = [ovh_dedicated_server_update.kimsufi_harddisk]
}

# ============================================================================
# TALOS MACHINE CONFIGURATION
# ============================================================================

locals {
  # OVH KS-5 control planes are also storage-bearing nodes. Keep them schedulable
  # so existing OVH-local workloads and local-path PVs can continue to run there.
  kimsufi_controlplane_cluster_config = merge(local.common_cluster_config, {
    allowSchedulingOnControlPlanes = true
  })

  # Per-node user-volume patches. KS-5 nodes expose /dev/sdb; KS-GAME nodes
  # expose a second NVMe. Both are mounted at the same path so local-path-ovh
  # can use OVH-local capacity uniformly.
  kimsufi_user_volume_config_patches = {
    for k, v in merge(local.kimsufi_servers, local.kimsufi_cp_servers) :
    k => v.data_disk_match == null ? [] : [
      yamlencode({
        apiVersion = "v1alpha1"
        kind       = "UserVolumeConfig"
        name       = "seaweedfs-data"
        volumeType = "disk"
        provisioning = {
          diskSelector = {
            match = v.data_disk_match
          }
        }
        filesystem = {
          type = "xfs"
        }
      })
    ]
  }

  kimsufi_controlplane_machine_config_patches = {
    for k, v in local.kimsufi_servers :
    k => yamlencode({
      machine = merge(local.common_machine_base, {
        install = {
          image = "factory.talos.dev/installer/${talos_image_factory_schematic.kimsufi.id}:${var.talos_version}"
        }
        files = local.cp_auth_files
        # Topology labels set explicitly. The installed talos-CCM only populates
        # ExternalIP / providerID when `--cloud-provider=external` is in the kubelet
        # extraArgs (see `kimsufi_cloud_provider_external_enabled_nodes` below); the
        # CCM transformation matches on `region=hil` set here.
        nodeLabels = {
          "topology.kubernetes.io/region" = "hil"
          "topology.kubernetes.io/zone"   = v.zone
        }
      })
      cluster = local.kimsufi_controlplane_cluster_config
    })
    if v.role == "controlplane"
  }

  kimsufi_worker_machine_config_patches = {
    for k, v in local.kimsufi_servers :
    k => yamlencode({
      machine = merge(local.worker_machine_base, {
        install = {
          image = "factory.talos.dev/installer/${talos_image_factory_schematic.kimsufi.id}:${var.talos_version}"
        }
        # Topology labels set explicitly — no CCM for OVH bare metal.
        nodeLabels = {
          "topology.kubernetes.io/region" = "hil"
          "topology.kubernetes.io/zone"   = v.zone
        }
      })
      cluster = local.worker_cluster_config
    })
    if v.role == "worker"
  }

  kimsufi_machine_config_patches = merge(
    local.kimsufi_controlplane_machine_config_patches,
    local.kimsufi_worker_machine_config_patches,
  )

  kimsufi_public_ips = merge(
    { for k, v in data.ovh_dedicated_server.kimsufi : k => v.ip },
    { for k, v in data.ovh_dedicated_server.kimsufi_cp : k => v.ip },
  )

  kimsufi_public_ipv4_24s = {
    for k, ip in local.kimsufi_public_ips : k => join(".", slice(split(".", ip), 0, 3))
  }

  kimsufi_eno1_peer_routes = {
    for k, ip in local.kimsufi_public_ips : k => [
      for peer_key, peer_ip in local.kimsufi_public_ips : {
        destination = "${peer_ip}/32"
        gateway     = "${local.kimsufi_public_ipv4_24s[k]}.254"
      }
      if peer_key != k && local.kimsufi_public_ipv4_24s[peer_key] == local.kimsufi_public_ipv4_24s[k]
    ]
  }

  # Roll this network change out one node at a time. A route-only LinkConfig
  # disables Talos' default DHCP operators, so each enabled node must get the
  # paired DHCPv4Config below and should be canaried with talosctl --mode=try
  # before being added here.
  kimsufi_eno1_peer_route_enabled_nodes = toset([
    "kimsufi_worker0",
    "kimsufi_worker1",
    "ks_game_worker0",
    "ks_game_worker1",
  ])

  kimsufi_eno1_peer_route_patches = {
    for k, routes in local.kimsufi_eno1_peer_routes : k => !contains(local.kimsufi_eno1_peer_route_enabled_nodes, k) || length(routes) == 0 ? [] : [
      yamlencode({
        apiVersion       = "v1alpha1"
        kind             = "DHCPv4Config"
        name             = "eno1"
        routeMetric      = 1024
        clientIdentifier = "mac"
      }),
      yamlencode({
        apiVersion = "v1alpha1"
        kind       = "LinkConfig"
        name       = "eno1"
        up         = true
        routes     = routes
      }),
    ]
  }

  # Kubelet --cloud-provider=external opt-in. The installed talos-CCM
  # (k8s/talos-cloud-controller-manager, configured with `publicIPDiscovery: true` for
  # `region=hil`) only acts on nodes whose kubelet was started with
  # `--cloud-provider=external` — that's the flag that makes kubelet apply the
  # `node.cloudprovider.kubernetes.io/uninitialized:NoSchedule` taint at first
  # registration, which is the CCM's input signal. Without it, the CCM short-circuits
  # ("is kubelet has args: --cloud-provider=external on the node?" in its log) and
  # Node.spec.providerID + status.addresses[ExternalIP] stay empty — which is why
  # tf/gitops/dns-records hard-codes IPs by hand. See cluster/docs/plan.md
  # "Autopopulate tf/gitops/dns-records IP lists" for the motivating goal.
  #
  # Kept Kimsufi-only deliberately: Proxmox nodes share `common_machine_base.kubelet`
  # but their `region=proxmox` doesn't match the CCM transformation's `region=hil`
  # selector, so flipping the flag there would leave them stuck with the
  # uninitialized taint and no CCM to clear it.
  #
  # MIGRATION NOTE: kubelet only adds the uninitialized taint at first node
  # registration, not on a flag-flip kubelet restart. Existing nodes that were
  # already registered before the flag landed need a one-time
  #
  #   kubectl taint nodes <node> node.cloudprovider.kubernetes.io/uninitialized=:NoSchedule
  #
  # after the kubelet restarts with the flag. The CCM then populates providerID +
  # ExternalIP and removes the taint, and the node is in steady state.
  # Fresh-bootstrapped nodes (post-flag install) get this for free.
  kimsufi_cloud_provider_external_enabled_nodes = toset([
    "kimsufi_cp0",
    "kimsufi_worker0",
    "kimsufi_worker1",
    "ks_game_worker0",
    "ks_game_worker1",
  ])

  kimsufi_cloud_provider_external_patches = {
    for k in concat(keys(local.active_kimsufi_servers), keys(local.active_kimsufi_cp_servers)) :
    k => contains(local.kimsufi_cloud_provider_external_enabled_nodes, k) ? [
      yamlencode({
        machine = {
          kubelet = {
            extraArgs = {
              "cloud-provider" = "external"
            }
          }
        }
      }),
    ] : []
  }

  kimsufi_cp_machine_config_patches = {
    for k, v in local.kimsufi_cp_servers :
    k => yamlencode({
      machine = merge(local.common_machine_base, {
        install = {
          image = "factory.talos.dev/installer/${talos_image_factory_schematic.kimsufi.id}:${var.talos_version}"
        }
        files = local.cp_auth_files
        # Topology labels set explicitly. The installed talos-CCM only populates
        # ExternalIP / providerID when `--cloud-provider=external` is in the kubelet
        # extraArgs (see `kimsufi_cloud_provider_external_enabled_nodes` below); the
        # CCM transformation matches on `region=hil` set here.
        nodeLabels = {
          "topology.kubernetes.io/region" = "hil"
          "topology.kubernetes.io/zone"   = v.zone
        }
      })
      cluster = local.kimsufi_controlplane_cluster_config
    })
  }
}

data "talos_machine_configuration" "kimsufi" {
  for_each = local.active_kimsufi_servers

  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  machine_secrets    = local.machine_secrets
  machine_type       = each.value.role
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
  examples           = false
  docs               = false

  config_patches = concat(
    [
      local.kimsufi_machine_config_patches[each.key],
      yamlencode({
        apiVersion = "v1alpha1"
        kind       = "HostnameConfig"
        auto       = "off"
        hostname   = each.value.hostname
      }),
    ],
    local.kimsufi_user_volume_config_patches[each.key],
    local.kimsufi_eno1_peer_route_patches[each.key],
    local.kimsufi_cloud_provider_external_patches[each.key],
    local.nebula_machine_patches[each.key],
  )
}

resource "talos_machine_configuration_apply" "kimsufi" {
  for_each = local.active_kimsufi_servers

  client_configuration        = local.client_configuration
  machine_configuration_input = data.talos_machine_configuration.kimsufi[each.key].machine_configuration
  apply_mode                  = try(each.value.apply_mode, "auto")
  node                        = data.ovh_dedicated_server.kimsufi[each.key].ip

  depends_on = [ovh_dedicated_server_reboot_task.kimsufi_to_talos]
}

# ============================================================================
# KIMSUFI CONTROL PLANE — provisioning resources
# ============================================================================

resource "ovh_dedicated_server" "kimsufi_cp" {
  for_each = local.active_kimsufi_cp_servers

  service_name        = each.value.service_name
  rescue_ssh_key      = data.sops_file.ovh_rescue_ssh.data["public_key"]
  display_name        = each.value.service_name
  efi_bootloader_path = "\\efi\\boot\\bootx64.efi"
}

resource "ovh_dedicated_server_update" "kimsufi_cp_rescue" {
  for_each     = local.active_kimsufi_cp_servers
  service_name = each.value.service_name
  boot_id      = local.kimsufi_rescue_boot_id
  depends_on   = [ovh_dedicated_server.kimsufi_cp]

  lifecycle {
    ignore_changes = [boot_id]
  }
}

resource "ovh_dedicated_server_reboot_task" "kimsufi_cp_to_rescue" {
  for_each     = local.active_kimsufi_cp_servers
  service_name = each.value.service_name
  keepers      = [tostring(local.kimsufi_rescue_boot_id)]
  depends_on   = [ovh_dedicated_server_update.kimsufi_cp_rescue]
}

resource "null_resource" "install_talos_kimsufi_cp" {
  for_each = local.active_kimsufi_cp_servers

  triggers = {
    schematic_id = talos_image_factory_schematic.kimsufi.id
  }

  connection {
    type        = "ssh"
    host        = data.ovh_dedicated_server.kimsufi_cp[each.key].ip
    user        = "root"
    private_key = data.sops_file.ovh_rescue_ssh.data["private_key"]
    timeout     = "15m"
  }

  provisioner "remote-exec" {
    inline = [
      "set -ex",
      "apt-get update -qq && apt-get install -y -qq zstd",
      "URL='${data.talos_image_factory_urls.kimsufi.urls.disk_image}'",
      "wget -q -O /tmp/talos.bin \"$URL\"",
      "case \"$URL\" in *.zst) zstd -dc /tmp/talos.bin > /tmp/talos.raw ;; *.xz) xz -dc /tmp/talos.bin > /tmp/talos.raw ;; *) echo \"unknown compression in $URL\" >&2; exit 1 ;; esac",
      "test -s /tmp/talos.raw",
      "dd if=/tmp/talos.raw of=${each.value.install_disk} bs=4M status=progress",
      "sync",
    ]
  }

  depends_on = [ovh_dedicated_server_reboot_task.kimsufi_cp_to_rescue]
}

resource "ovh_dedicated_server_update" "kimsufi_cp_harddisk" {
  for_each     = local.active_kimsufi_cp_servers
  service_name = each.value.service_name
  boot_id      = local.kimsufi_harddisk_boot_id
  depends_on   = [null_resource.install_talos_kimsufi_cp]
}

resource "ovh_dedicated_server_reboot_task" "kimsufi_cp_to_talos" {
  for_each     = local.active_kimsufi_cp_servers
  service_name = each.value.service_name
  keepers      = [tostring(local.kimsufi_harddisk_boot_id)]
  depends_on   = [ovh_dedicated_server_update.kimsufi_cp_harddisk]
}

# ============================================================================
# KIMSUFI CONTROL PLANE — Talos machine configuration
# ============================================================================

data "talos_machine_configuration" "kimsufi_cp" {
  for_each = local.active_kimsufi_cp_servers

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
      local.kimsufi_cp_machine_config_patches[each.key],
      yamlencode({
        apiVersion = "v1alpha1"
        kind       = "HostnameConfig"
        auto       = "off"
        hostname   = each.value.hostname
      }),
    ],
    local.kimsufi_user_volume_config_patches[each.key],
    local.kimsufi_eno1_peer_route_patches[each.key],
    local.kimsufi_cloud_provider_external_patches[each.key],
    local.nebula_machine_patches[each.key],
  )
}

resource "talos_machine_configuration_apply" "kimsufi_cp" {
  for_each = local.active_kimsufi_cp_servers

  client_configuration        = local.client_configuration
  machine_configuration_input = data.talos_machine_configuration.kimsufi_cp[each.key].machine_configuration
  node                        = data.ovh_dedicated_server.kimsufi_cp[each.key].ip

  depends_on = [ovh_dedicated_server_reboot_task.kimsufi_cp_to_talos]
}
