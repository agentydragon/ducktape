# Terraform configuration for k3s cluster on Proxmox
# This manages the complete lifecycle of k3s VMs

terraform {
  required_providers {
    proxmox = {
      source  = "Telmate/proxmox"
      version = "~> 2.9"
    }
  }
}

provider "proxmox" {
  pm_api_url      = "https://atlas:8006/api2/json"
  pm_user         = "root@pam"
  pm_password     = var.proxmox_password
  pm_tls_insecure = true
}

variable "proxmox_password" {
  type      = string
  sensitive = true
}

# Master node
resource "proxmox_vm_qemu" "k3s_master" {
  name        = "k3s-master"
  target_node = "atlas"
  vmid        = 200
  
  clone = "ubuntu-22.04-cloudinit-template"
  
  cores   = 2
  sockets = 1
  memory  = 4096
  
  disk {
    size    = "50G"
    type    = "scsi"
    storage = "local-zfs"
  }
  
  network {
    model  = "virtio"
    bridge = "vmbr0"
  }
  
  ipconfig0 = "ip=10.0.200.200/16,gw=10.0.0.1"
  
  # Cloud-init user data
  cicustom = "user=local:snippets/k3s-master-cloud-init.yml"
  
  # SSH key for management (optional, not from atlas)
  sshkeys = file("~/.ssh/id_ed25519.pub")
  
  lifecycle {
    ignore_changes = [
      disk,  # Prevent recreation on disk size changes
    ]
  }
}

# Worker node
resource "proxmox_vm_qemu" "k3s_worker" {
  name        = "k3s-worker"
  target_node = "atlas"
  vmid        = 201
  
  clone = "ubuntu-22.04-cloudinit-template"
  
  cores   = 2
  sockets = 1
  memory  = 4096
  
  disk {
    size    = "50G"
    type    = "scsi"
    storage = "local-zfs"
  }
  
  network {
    model  = "virtio"
    bridge = "vmbr0"
  }
  
  ipconfig0 = "ip=10.0.200.201/16,gw=10.0.0.1"
  
  cicustom = "user=local:snippets/k3s-worker-cloud-init.yml"
  
  sshkeys = file("~/.ssh/id_ed25519.pub")
  
  # Ensure master starts first
  depends_on = [proxmox_vm_qemu.k3s_master]
  
  lifecycle {
    ignore_changes = [disk]
  }
}

output "master_ip" {
  value = "10.0.200.200"
}

output "worker_ip" {
  value = "10.0.200.201"
}

output "kubeconfig_instructions" {
  value = <<EOF
To access the cluster:
1. Get kubeconfig from master: scp ubuntu@10.0.200.200:/etc/rancher/k3s/k3s.yaml ~/.kube/config-k3s
2. Update server URL: sed -i 's/127.0.0.1/10.0.200.200/' ~/.kube/config-k3s
3. Export: export KUBECONFIG=~/.kube/config-k3s
EOF
}