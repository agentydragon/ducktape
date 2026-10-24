# Host preparation only. Piraeus eligibility is controlled separately and must be
# activated only after the target's rebuild. Keep the rescue/dd schematic unchanged.
locals {
  # No node carries the FILE_THIN canary label: its pool directory lived on the
  # legacy local-path data volume, which no remaining node has.
  drbd_canary_nodes          = toset([])
  drbd_lvm_thin_nodes        = local.piraeus_talos_storage_enabled_nodes
  drbd_prepared_nodes        = setunion(local.drbd_canary_nodes, local.drbd_lvm_thin_nodes)
  drbd_canary_talos_version  = "v1.14.0"
  drbd_canary_base_schematic = yamldecode(talos_image_factory_schematic.kimsufi.schematic)

  drbd_canary_installer_image = "factory.talos.dev/metal-installer/${talos_image_factory_schematic.drbd_canary.id}:${local.drbd_canary_talos_version}"

  drbd_canary_machine_config_patches = {
    for name in keys(local.active_kimsufi_servers) :
    name => contains(local.drbd_prepared_nodes, name) ? [
      yamlencode({
        machine = merge(
          {
            install = {
              image = local.drbd_canary_installer_image
            }
            kernel = {
              modules = concat(
                [
                  { name = "drbd", parameters = ["usermode_helper=disabled"] },
                  { name = "drbd_transport_tcp" },
                ],
                contains(local.drbd_lvm_thin_nodes, name) ? [
                  { name = "dm_thin_pool" },
                  { name = "dm_snapshot" },
                ] : [],
              )
            }
          },
          contains(local.drbd_canary_nodes, name) ? {
            nodeLabels = {
              "storage.allegedly.works/drbd-canary" = "true"
            }
          } : {},
        )
      }),
    ] : []
  }
}

resource "talos_image_factory_schematic" "drbd_canary" {
  schematic = yamlencode(merge(local.drbd_canary_base_schematic, {
    customization = merge(local.drbd_canary_base_schematic.customization, {
      systemExtensions = {
        officialExtensions = concat(
          local.drbd_canary_base_schematic.customization.systemExtensions.officialExtensions,
          ["siderolabs/drbd"],
        )
      }
    })
  }))
}

output "drbd_canary_installer_image" {
  description = "In-place upgrade image for DRBD-prepared OVH Talos nodes; never use the rescue/dd installer"
  value       = local.drbd_canary_installer_image
}
