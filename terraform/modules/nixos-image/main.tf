# NixOS Image Module
# Builds a per-host qcow2 image via `nix build` and uploads it to Proxmox.
# Uses system.build.images.qemu-efi (nixos-generators upstreamed in nixpkgs 25.05+).

resource "null_resource" "image" {
  triggers = {
    # Rebuild when any nix config changes (NixOS + HM are baked into the image)
    nix_dir_hash = var.nix_dir_hash
    proxmox_host = var.proxmox_host
    flake_target = var.flake_target
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      echo "Building ${var.flake_target} NixOS qcow2 image..."
      cd "${var.repo_root}"
      nix build ./nix#${var.flake_target}-image -o ${var.flake_target}-image

      echo "Uploading to Proxmox..."
      ssh root@${var.proxmox_host} "mkdir -p /var/lib/vz/import"
      scp ${var.flake_target}-image/*.qcow2 "root@${var.proxmox_host}:/var/lib/vz/import/${var.flake_target}.qcow2"
      echo "${var.flake_target} image ready at local:import/${var.flake_target}.qcow2"
    EOT
  }
}
