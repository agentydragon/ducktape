# DRBD and LVM_THIN host preparation for Piraeus on every OVH Talos node: the DRBD
# extension (in-place upgrade image) and kernel modules. Keep the rescue/dd schematic unchanged.
locals {
  drbd_talos_version  = "v1.14.0"
  drbd_base_schematic = yamldecode(talos_image_factory_schematic.kimsufi.schematic)

  drbd_installer_image = "factory.talos.dev/metal-installer/${talos_image_factory_schematic.drbd.id}:${local.drbd_talos_version}"

  drbd_kernel_modules = [
    { name = "drbd", parameters = ["usermode_helper=disabled"] },
    { name = "drbd_transport_tcp" },
    { name = "dm_thin_pool" },
    { name = "dm_snapshot" },
  ]

  drbd_machine_config_patches = {
    for name in keys(local.active_kimsufi_servers) :
    name => [
      yamlencode({
        machine = {
          install = {
            image = local.drbd_installer_image
          }
          kernel = {
            modules = local.drbd_kernel_modules
          }
        }
      }),
    ]
  }
}

moved {
  from = talos_image_factory_schematic.drbd_canary
  to   = talos_image_factory_schematic.drbd
}

resource "talos_image_factory_schematic" "drbd" {
  schematic = yamlencode(merge(local.drbd_base_schematic, {
    customization = merge(local.drbd_base_schematic.customization, {
      systemExtensions = {
        officialExtensions = concat(
          local.drbd_base_schematic.customization.systemExtensions.officialExtensions,
          ["siderolabs/drbd"],
        )
      }
    })
  }))
}

output "drbd_installer_image" {
  description = "In-place upgrade image for DRBD-prepared Talos nodes; never use the rescue/dd installer"
  value       = local.drbd_installer_image
}
