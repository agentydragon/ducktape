# Host preparation only. Piraeus/CSI is deployed separately after the module checks
# in cluster/docs/drbd_canary.md. Keep the rescue/dd schematic unchanged.
locals {
  drbd_canary_nodes          = toset(["ovh-ns102453", "ovh-ns103656"])
  drbd_canary_talos_version  = "v1.14.0"
  drbd_canary_base_schematic = yamldecode(talos_image_factory_schematic.kimsufi.schematic)

  drbd_canary_installer_image = "factory.talos.dev/metal-installer/${talos_image_factory_schematic.drbd_canary.id}:${local.drbd_canary_talos_version}"

  drbd_canary_machine_config_patches = {
    for name in keys(local.active_kimsufi_servers) :
    name => contains(local.drbd_canary_nodes, name) ? [
      yamlencode({
        machine = {
          install = {
            image = local.drbd_canary_installer_image
          }
          kernel = {
            modules = [
              { name = "drbd", parameters = ["usermode_helper=disabled"] },
              { name = "drbd_transport_tcp" },
            ]
          }
          nodeLabels = {
            "storage.allegedly.works/drbd-canary" = "true"
          }
        }
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
  description = "In-place upgrade image for the DRBD canary workers; never use the rescue/dd installer"
  value       = local.drbd_canary_installer_image
}
