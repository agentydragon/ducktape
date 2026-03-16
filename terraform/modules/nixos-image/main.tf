# NixOS Image Module
# Builds a per-host qcow2 image via `nix build` and uploads it to Proxmox.
# Uses system.build.images.qemu-efi (nixos-generators upstreamed in nixpkgs 25.05+).

terraform {
  required_version = ">= 1.0"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = ">= 2.3.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2.0"
    }
  }
}

# Step 1: Fail fast if SSH to the Proxmox host doesn't work.
# Runs before the (slow) nix build so we don't waste time building an image
# we can't upload.
data "external" "ssh_check" {
  program = ["bash", "-c", <<-EOT
    if ssh -o BatchMode=yes -o ConnectTimeout=5 root@${var.proxmox_host} true 2>/dev/null; then
      printf '{"status":"ok"}'
    else
      echo "ERROR: SSH to root@${var.proxmox_host} failed." >&2
      echo "Ensure your SSH key is loaded (ssh-add) and the host key is in known_hosts." >&2
      exit 1
    fi
  EOT
  ]
}

# Step 2: Build the NixOS qcow2 image.
# Output symlink goes to /tmp so it doesn't pollute the repo working tree.
resource "null_resource" "build" {
  triggers = {
    nix_dir_hash = var.nix_dir_hash
    flake_target = var.flake_target
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      echo "Building ${var.flake_target} NixOS qcow2 image..."
      cd "${var.repo_root}"
      nix build .#${var.flake_target}-image -o /tmp/${var.flake_target}-image
    EOT
  }

  depends_on = [data.external.ssh_check]
}

# Step 3: Upload the built image to Proxmox.
resource "null_resource" "upload" {
  triggers = {
    nix_dir_hash = var.nix_dir_hash
    proxmox_host = var.proxmox_host
    flake_target = var.flake_target
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      echo "Uploading ${var.flake_target} image to Proxmox..."
      ssh root@${var.proxmox_host} "mkdir -p /var/lib/vz/import"
      scp /tmp/${var.flake_target}-image/*.qcow2 "root@${var.proxmox_host}:/var/lib/vz/import/${var.flake_target}.qcow2"
      echo "${var.flake_target} image ready at local:import/${var.flake_target}.qcow2"
    EOT
  }

  depends_on = [null_resource.build]
}
