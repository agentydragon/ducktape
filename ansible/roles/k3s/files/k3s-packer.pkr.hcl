# Packer template for building k3s-ready VM images
# This creates a template with k3s pre-installed, no SSH needed

source "proxmox" "k3s-base" {
  proxmox_url              = "https://atlas:8006/api2/json"
  username                 = "root@pam"
  password                 = var.proxmox_password
  node                     = "atlas"
  
  clone_vm_id              = 9000  # Ubuntu cloud-init template
  vm_name                  = "k3s-ready-template"
  vm_id                    = 9001
  
  cores                    = 2
  memory                   = 2048
  
  ssh_username             = "ubuntu"
  ssh_private_key_file     = "~/.ssh/id_ed25519"
  ssh_timeout              = "20m"
  
  template_name            = "k3s-ready-template"
  template_description     = "Ubuntu 22.04 with k3s dependencies pre-installed"
}

build {
  sources = ["source.proxmox.k3s-base"]
  
  provisioner "shell" {
    inline = [
      "sudo apt-get update",
      "sudo apt-get install -y curl ca-certificates",
      "sudo mkdir -p /etc/rancher/k3s",
      "curl -sfL https://get.k3s.io > /tmp/install-k3s.sh",
      "chmod +x /tmp/install-k3s.sh"
    ]
  }
  
  provisioner "file" {
    content = <<EOF
mirrors:
  "registry.registry.svc.cluster.local:5000":
    endpoint:
      - "http://registry.registry.svc.cluster.local:5000"
EOF
    destination = "/tmp/registries.yaml"
  }
  
  provisioner "shell" {
    inline = [
      "sudo mv /tmp/registries.yaml /etc/rancher/k3s/registries.yaml",
      "sudo chown root:root /etc/rancher/k3s/registries.yaml"
    ]
  }
}