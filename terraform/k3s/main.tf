# Terraform configuration for k3s cluster on Proxmox
# Simple setup - just terraform init, plan, apply

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
  pm_user         = "terraform@pam"  # Create this user in Proxmox
  pm_password     = var.proxmox_password
  pm_tls_insecure = true  # Atlas uses self-signed cert
}

variable "proxmox_password" {
  description = "Password for Proxmox terraform user"
  type        = string
  sensitive   = true
  # Set via environment: export TF_VAR_proxmox_password="..."
}

# Configuration variables
locals {
  k3s_version      = "v1.29.0+k3s1"
  cluster_cidr     = "10.42.0.0/16"
  service_cidr     = "10.43.0.0/16"
  cluster_dns      = "10.43.0.10"
  network_gateway  = "10.0.0.1"
  ssh_public_key   = file("~/.ssh/id_ed25519.pub")
}

# Master node
resource "proxmox_vm_qemu" "k3s_master" {
  name        = "k3s-master"
  target_node = "atlas"
  vmid        = 200
  
  # Clone from existing template
  clone = "ubuntu-22.04-cloudinit-template"
  full_clone = true
  
  # Hardware
  cores   = 2
  sockets = 1
  memory  = 4096
  
  # Disk
  disk {
    size    = "50G"
    type    = "scsi"
    storage = "local-zfs"
  }
  
  # Network
  network {
    model  = "virtio"
    bridge = "vmbr0"
  }
  
  # Static IP
  ipconfig0 = "ip=10.0.200.200/16,gw=${local.network_gateway}"
  
  # SSH key for direct access from your laptop
  sshkeys = local.ssh_public_key
  
  # Cloud-init user
  ciuser = "ubuntu"
  
  # Start on boot
  onboot = true
  
  # Startup script via cloud-init
  # Note: Proxmox provider has limited cloud-init support
  # For full cloud-init, we'll use provisioners below
}

# Worker node
resource "proxmox_vm_qemu" "k3s_worker" {
  name        = "k3s-worker"
  target_node = "atlas"
  vmid        = 201
  
  clone      = "ubuntu-22.04-cloudinit-template"
  full_clone = true
  
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
  
  ipconfig0 = "ip=10.0.200.201/16,gw=${local.network_gateway}"
  sshkeys   = local.ssh_public_key
  ciuser    = "ubuntu"
  onboot    = true
  
  # Ensure master is created first
  depends_on = [proxmox_vm_qemu.k3s_master]
}

# Wait for VMs to be accessible
resource "null_resource" "wait_for_vms" {
  depends_on = [
    proxmox_vm_qemu.k3s_master,
    proxmox_vm_qemu.k3s_worker
  ]
  
  provisioner "local-exec" {
    command = <<-EOT
      echo "Waiting for VMs to be accessible..."
      until nc -zv 10.0.200.200 22 2>/dev/null; do sleep 5; done
      until nc -zv 10.0.200.201 22 2>/dev/null; do sleep 5; done
      echo "VMs are up!"
    EOT
  }
}

# Install k3s on master
resource "null_resource" "k3s_master_install" {
  depends_on = [null_resource.wait_for_vms]
  
  connection {
    type        = "ssh"
    host        = "10.0.200.200"
    user        = "ubuntu"
    private_key = file("~/.ssh/id_ed25519")
  }
  
  # Install k3s
  provisioner "remote-exec" {
    inline = [
      "curl -sfL https://get.k3s.io | sh -s - --cluster-cidr=${local.cluster_cidr} --service-cidr=${local.service_cidr} --cluster-dns=${local.cluster_dns} --disable traefik --write-kubeconfig-mode 644",
      "sudo cat /var/lib/rancher/k3s/server/node-token > /tmp/node-token",
      "sudo chmod 644 /tmp/node-token"
    ]
  }
}

# Get node token from master
data "external" "k3s_token" {
  depends_on = [null_resource.k3s_master_install]
  
  program = ["bash", "-c", "echo '{\"token\":\"'$(ssh -o StrictHostKeyChecking=no ubuntu@10.0.200.200 cat /tmp/node-token)'\"}'"]
}

# Install k3s on worker
resource "null_resource" "k3s_worker_install" {
  depends_on = [data.external.k3s_token]
  
  connection {
    type        = "ssh"
    host        = "10.0.200.201"
    user        = "ubuntu"
    private_key = file("~/.ssh/id_ed25519")
  }
  
  provisioner "remote-exec" {
    inline = [
      "curl -sfL https://get.k3s.io | K3S_URL=https://10.0.200.200:6443 K3S_TOKEN=${data.external.k3s_token.result.token} sh -"
    ]
  }
}

# Output connection info
output "master_ip" {
  value = "10.0.200.200"
}

output "worker_ip" {
  value = "10.0.200.201"
}

output "kubeconfig_command" {
  value = "scp ubuntu@10.0.200.200:/etc/rancher/k3s/k3s.yaml ~/.kube/config-k3s && sed -i 's/127.0.0.1/10.0.200.200/' ~/.kube/config-k3s"
}

output "next_steps" {
  value = <<-EOT
    Cluster is ready! To use it:
    
    1. Get kubeconfig:
       ${self.kubeconfig_command}
    
    2. Use the cluster:
       export KUBECONFIG=~/.kube/config-k3s
       kubectl get nodes
  EOT
}