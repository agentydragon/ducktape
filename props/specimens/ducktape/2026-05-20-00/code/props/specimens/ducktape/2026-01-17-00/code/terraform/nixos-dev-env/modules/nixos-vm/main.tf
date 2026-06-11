# NixOS VM Module
# Creates a NixOS VM with cloud-init provisioning
# Configuration is managed via flake after initial bootstrap

terraform {
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
  }
}

locals {
  cloud_init_user_data = templatefile("${path.module}/../../cloud-init.yaml.tpl", {
    username               = var.username
    ssh_public_key         = var.ssh_public_key
    hostname               = var.vm_name
    nixos_flake_url        = var.nixos_flake_url
    nixos_host             = var.nixos_host
    home_manager_flake_url = var.home_manager_flake_url
    home_manager_host      = var.home_manager_host
  })
}

# Cloud-init configuration
resource "proxmox_virtual_environment_file" "cloud_init_config" {
  content_type = "snippets"
  datastore_id = "local"
  node_name    = var.proxmox_node_name

  source_raw {
    data      = local.cloud_init_user_data
    file_name = "${var.vm_name}-cloud-init.yaml"
  }
}

# The VM
resource "proxmox_virtual_environment_vm" "vm" {
  name        = var.vm_name
  description = "NixOS VM - managed via flake ${var.nixos_flake_url}#${var.nixos_host}"
  node_name   = var.proxmox_node_name
  vm_id       = var.vm_id
  pool_id     = var.pool_id
  bios        = "ovmf" # UEFI boot required for qcow-efi images

  cpu {
    cores = var.vcpus
    type  = "host"
  }

  memory {
    dedicated = var.memory_mb
  }

  efi_disk {
    datastore_id = var.storage
    file_format  = "raw"
    type         = "4m"
  }

  disk {
    datastore_id = var.storage
    import_from  = "local:import/nixos-cloud.qcow2"
    interface    = "scsi0"
    iothread     = true
    discard      = "on"
    size         = var.disk_size_gb
  }

  network_device {
    bridge = var.network_bridge
    model  = "virtio"
  }

  initialization {
    datastore_id = var.storage
    interface    = "sata0"

    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }

    user_account {
      username = var.username
      keys     = var.ssh_public_key != "" ? [var.ssh_public_key] : []
      password = ""
    }

    user_data_file_id = proxmox_virtual_environment_file.cloud_init_config.id
  }

  started = var.auto_start

  agent {
    enabled = true
    timeout = "10m" # Wait longer for guest agent to report IP (cloud-init takes time)
  }

  # Ignore changes to cloud-init after creation - updates happen via nixos-rebuild
  lifecycle {
    ignore_changes = [
      initialization[0].user_data_file_id,
    ]
  }
}

# Note: Initial NixOS and home-manager setup is done by cloud-init
# The provisioners below are commented out because:
# 1. On first create, cloud-init already handles the flake setup
# 2. The VM's ipv4_addresses isn't available until after QEMU agent reports
# 3. For updates, use: terraform apply -var="rebuild_trigger=$(date +%s)"
#    or SSH directly: ssh user@<ip> 'sudo nixos-rebuild switch --flake ...'
#
# TODO: Consider using a time_sleep resource or external data source to wait
# for IP availability if automatic terraform-driven updates are needed.

# Trigger NixOS rebuild when flake config changes
# Disabled on initial create - cloud-init handles this
# resource "null_resource" "nixos_rebuild" {
#   triggers = {
#     nixos_flake_url = var.nixos_flake_url
#     nixos_host      = var.nixos_host
#     rebuild_trigger = var.rebuild_trigger
#   }
#
#   provisioner "remote-exec" {
#     inline = [
#       "echo 'Rebuilding NixOS from flake...'",
#       "sudo nixos-rebuild switch --flake ${var.nixos_flake_url}#${var.nixos_host}",
#     ]
#
#     connection {
#       type        = "ssh"
#       user        = var.username
#       host        = proxmox_virtual_environment_vm.vm.ipv4_addresses[1][0]
#       agent       = true
#       timeout     = "5m"
#     }
#   }
#
#   depends_on = [proxmox_virtual_environment_vm.vm]
# }
#
# resource "null_resource" "home_manager" {
#   triggers = {
#     home_manager_flake_url = var.home_manager_flake_url
#     home_manager_host      = var.home_manager_host
#     rebuild_trigger        = var.rebuild_trigger
#   }
#
#   provisioner "remote-exec" {
#     inline = [
#       "echo 'Rebuilding home-manager from flake...'",
#       "home-manager switch --flake ${var.home_manager_flake_url}#${var.home_manager_host}",
#     ]
#
#     connection {
#       type        = "ssh"
#       user        = var.username
#       host        = proxmox_virtual_environment_vm.vm.ipv4_addresses[1][0]
#       agent       = true
#       timeout     = "5m"
#     }
#   }
#
#   depends_on = [null_resource.nixos_rebuild]
# }
