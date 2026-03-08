# NixOS VM Module
# Creates a NixOS VM from a pre-built qcow2 image.
# SSH keys are injected via Proxmox cloud-init.
# K8s secrets are optionally injected via cloud-init userdata.

terraform {
  required_version = ">= 1.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.91.0"
    }
  }
}

locals {
  # Cloud-init userdata is only needed for k8s secret injection
  needs_cloud_init_userdata = var.k8s_cluster_join != null
  cloud_init_user_data = local.needs_cloud_init_userdata ? templatefile("${path.module}/cloud-init.yaml.tpl", {
    k8s_cluster_join = var.k8s_cluster_join
  }) : null
}

# Cloud-init configuration (only for k8s secret injection)
resource "proxmox_virtual_environment_file" "cloud_init_config" {
  count        = local.needs_cloud_init_userdata ? 1 : 0
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
  description = "NixOS VM - ${var.vm_name}"
  node_name   = var.proxmox_node_name
  vm_id       = var.vm_id
  pool_id     = var.pool_id != "" ? var.pool_id : null
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
    import_from  = var.image_import_path
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

    user_data_file_id = local.needs_cloud_init_userdata ? proxmox_virtual_environment_file.cloud_init_config[0].id : null
  }

  started = var.auto_start

  agent {
    enabled = true
    timeout = "2m"
  }

  # Ignore changes to cloud-init after creation - updates happen via nixos-rebuild
  lifecycle {
    ignore_changes = [
      initialization[0].user_data_file_id,
    ]
  }
}
