# Libvirt VM Module
# Creates a VM from a pre-built qcow2 image on a local QEMU/KVM host.
# SSH keys and optional cloud-init userdata are injected via NoCloud ISO.

terraform {
  required_version = ">= 1.0"

  required_providers {
    libvirt = {
      source  = "dmacvicar/libvirt"
      version = ">= 0.8.0"
    }
  }
}

# Base volume from pre-built qcow2
resource "libvirt_volume" "os_image" {
  name   = "${var.vm_name}-base.qcow2"
  pool   = var.storage_pool
  source = var.qcow2_image_path
  format = "qcow2"
}

# CoW overlay disk with desired size
resource "libvirt_volume" "os_disk" {
  name           = "${var.vm_name}.qcow2"
  pool           = var.storage_pool
  base_volume_id = libvirt_volume.os_image.id
  format         = "qcow2"
  size           = var.disk_size_gb * 1024 * 1024 * 1024
}

# NoCloud cloud-init ISO (only when cloud_init_user_data is provided)
resource "libvirt_cloudinit_disk" "cloud_init" {
  count     = var.cloud_init_user_data != null ? 1 : 0
  name      = "${var.vm_name}-cloudinit.iso"
  pool      = var.storage_pool
  user_data = var.cloud_init_user_data
}

# The VM
resource "libvirt_domain" "vm" {
  name   = var.vm_name
  vcpu   = var.vcpus
  memory = var.memory_mb

  cpu {
    mode = "host-passthrough"
  }

  firmware = var.uefi_firmware_path

  disk {
    volume_id = libvirt_volume.os_disk.id
    scsi      = true
  }

  dynamic "disk" {
    for_each = var.cloud_init_user_data != null ? [1] : []
    content {
      volume_id = libvirt_cloudinit_disk.cloud_init[0].id
    }
  }

  network_interface {
    network_name   = var.network_name
    wait_for_lease = true
  }

  console {
    type        = "pty"
    target_type = "serial"
    target_port = "0"
  }

  graphics {
    type        = "vnc"
    listen_type = "address"
  }

  autostart = var.auto_start

  lifecycle {
    ignore_changes = [
      # Cloud-init is one-shot; don't re-create VM on template changes
      disk,
    ]
  }
}
