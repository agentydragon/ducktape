# Home bare-metal Talos workers.
#
# These machines are installed locally from a Talos ISO, unlike OVH nodes whose
# disk lifecycle is driven through rescue mode. Tofu owns the complete machine
# configuration; the initial delivery to maintenance mode remains an explicit
# operator step documented in cluster/docs/optiplex_provisioning.md.

locals {
  home_nodes = {
    optiplex = {
      hostname     = "optiplex"
      nebula_ip    = "10.42.0.18"
      install_disk = "/dev/disk/by-id/nvme-BC511_NVMe_SK_hynix_256GB_AS9CN54631CA0CT13"
      region       = "home"
      zone         = "home-lan"
    }
  }

  home_worker_machine_config_patches = {
    for name, node in local.home_nodes :
    name => yamlencode({
      machine = merge(local.worker_machine_base, {
        install = {
          disk  = node.install_disk
          image = "factory.talos.dev/installer/${talos_image_factory_schematic.kimsufi.id}:${var.talos_version}"
        }
        nodeLabels = {
          "topology.kubernetes.io/region" = node.region
          "topology.kubernetes.io/zone"   = node.zone
          "storage.allegedly.works/tier"  = "ssd"
        }
      })
      cluster = local.worker_cluster_config
    })
  }
}

data "talos_machine_configuration" "home_worker" {
  for_each = local.home_nodes

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
      local.home_worker_machine_config_patches[each.key],
      yamlencode({
        apiVersion = "v1alpha1"
        kind       = "HostnameConfig"
        auto       = "off"
        hostname   = each.value.hostname
      }),
    ],
    local.nebula_machine_patches[each.key],
    [local.talos_node_logging_patch],
  )
}
