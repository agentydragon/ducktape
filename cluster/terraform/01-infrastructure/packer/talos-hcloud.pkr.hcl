# Packer template: Create Hetzner Cloud snapshot with Talos pre-installed on disk.
#
# Boots a temporary server in rescue mode, downloads the Talos disk image from
# Image Factory, writes it to /dev/sda, and takes a snapshot. This eliminates
# the dual-identity problem from ISO-to-disk reboot (phantom KubeSpan peers).
#
# Called by terraform_data.talos_hcloud_image in hetzner-nodes.tf.

packer {
  required_plugins {
    hcloud = {
      version = ">= 1.6.0"
      source  = "github.com/hetznercloud/hcloud"
    }
  }
}

variable "talos_image_url" {
  type        = string
  description = "URL to the Talos raw.xz disk image from Image Factory"
}

variable "talos_version" {
  type    = string
  default = "v1.12.3"
}

variable "schematic_id" {
  type        = string
  description = "Talos Image Factory schematic ID (for snapshot labeling)"
}

variable "server_location" {
  type    = string
  default = "hil"
}

source "hcloud" "talos" {
  rescue       = "linux64"
  image        = "debian-12"
  location     = var.server_location
  server_type  = "cpx11"
  ssh_username = "root"

  snapshot_name = "talos-${var.talos_version}-${substr(var.schematic_id, 0, 8)}"
  snapshot_labels = {
    os           = "talos"
    version      = var.talos_version
    schematic_id = substr(var.schematic_id, 0, 8)
  }
}

build {
  sources = ["source.hcloud.talos"]

  provisioner "shell" {
    inline = [
      "set -ex",
      "wget --timeout=30 --tries=5 --retry-connrefused -O /tmp/talos.raw.xz '${var.talos_image_url}'",
      "xz -d -c /tmp/talos.raw.xz | dd of=/dev/sda bs=4M && sync",
    ]
  }
}
