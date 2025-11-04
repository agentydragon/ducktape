# SAFE IMPORT CONFIG - Manages existing k3s VMs without modifications
# This config is designed to import existing VMs without any changes

terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.66"
    }
  }
}

provider "proxmox" {
  endpoint = "https://atlas:8006/"
  insecure = true  # Skip TLS verification for self-signed cert
  
  # API token authentication (provided via TF_VAR_proxmox_api_token)
  api_token = var.proxmox_api_token
  
  # SSH for certain operations
  ssh {
    agent    = true      # Use SSH agent
    username = "root"    # SSH as root to atlas
  }
  
  # Temporary for performance during import
  tmp_dir = "/var/tmp"
}

variable "proxmox_api_token" {
  description = "Proxmox API token in format: user@realm!token-id=secret"
  type        = string
  sensitive   = true
}

# Master node - configured to match existing VM exactly
resource "proxmox_virtual_environment_vm" "k3s_master" {
  name      = "k3s-master"
  node_name = "atlas"
  vm_id     = 200
  
  # Keep existing agent settings
  agent {
    enabled = true
    timeout = "15m"
    type    = "virtio"
  }
  
  # Preserve existing CPU configuration
  cpu {
    cores   = 2
    sockets = 1
    units   = 1024  # Keep current value
  }
  
  # Preserve existing memory
  memory {
    dedicated = 4096
  }
  
  # Serial console for access
  serial_device {
    device = "socket"
  }
  
  # Boot settings
  on_boot = true  # Start on host boot
  
  # Keyboard layout (default)
  keyboard_layout = "en-us"
  
  # Operating system info
  operating_system {
    type = "l26"  # Linux 2.6/3.x/4.x/5.x kernel
  }
  
  # Gradually allowing Terraform to manage safe attributes
  lifecycle {
    ignore_changes = [
      # Critical - NEVER change these (would recreate VM):
      vm_id,        # Changing VM ID would recreate
      node_name,    # Can't move VM to different node without migration
      clone,        # Don't try to re-clone from template
      
      # Disks - be very careful:
      disk,         # Don't modify disks (data loss risk)
      
      # Network - changing these could break k3s:
      network_device,  # Don't change network config
      initialization,  # Don't change cloud-init (includes IPs)
      
      # Settings we might want to manage later:
      # cpu,        # Could safely manage CPU
      # memory,     # Could safely manage RAM  
      # agent,      # Could manage guest agent settings
      # tags,       # Could add tags for organization
      # description # Could add descriptions
    ]
  }
}

# Worker node - configured to match existing VM exactly  
resource "proxmox_virtual_environment_vm" "k3s_worker" {
  name      = "k3s-worker"
  node_name = "atlas"
  vm_id     = 201
  
  # Keep existing agent settings
  agent {
    enabled = true
    timeout = "15m"
    type    = "virtio"
  }
  
  # Preserve existing CPU configuration
  cpu {
    cores   = 2
    sockets = 1
    units   = 1024  # Keep current value
  }
  
  # Preserve existing memory
  memory {
    dedicated = 4096
  }
  
  # Serial console for access
  serial_device {
    device = "socket"
  }
  
  # Boot settings
  on_boot = true  # Start on host boot
  
  # Keyboard layout (default)
  keyboard_layout = "en-us"
  
  # Operating system info
  operating_system {
    type = "l26"  # Linux 2.6/3.x/4.x/5.x kernel
  }
  
  lifecycle {
    ignore_changes = [
      # Critical - NEVER change these (would recreate VM):
      vm_id,        # Changing VM ID would recreate
      node_name,    # Can't move VM to different node without migration
      clone,        # Don't try to re-clone from template
      
      # Disks - be very careful:
      disk,         # Don't modify disks (data loss risk)
      
      # Network - changing these could break k3s:
      network_device,  # Don't change network config
      initialization,  # Don't change cloud-init (includes IPs)
      
      # Settings we might want to manage later:
      # cpu,        # Could safely manage CPU
      # memory,     # Could safely manage RAM  
      # agent,      # Could manage guest agent settings
      # tags,       # Could add tags for organization
      # description # Could add descriptions
    ]
  }
}

# Outputs to verify we're managing the right VMs
output "managed_vms" {
  value = {
    master = {
      vmid = proxmox_virtual_environment_vm.k3s_master.vm_id
      name = proxmox_virtual_environment_vm.k3s_master.name
    }
    worker = {
      vmid = proxmox_virtual_environment_vm.k3s_worker.vm_id  
      name = proxmox_virtual_environment_vm.k3s_worker.name
    }
  }
}

output "warning" {
  value = "This Terraform config is for IMPORT ONLY. Do not apply without importing first!"
}