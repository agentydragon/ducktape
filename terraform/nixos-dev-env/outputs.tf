# Outputs for NixOS Dev Environment

output "pool_id" {
  description = "Resource pool ID"
  value       = proxmox_virtual_environment_pool.user_pool.pool_id
}

output "username" {
  description = "Proxmox username"
  value       = local.proxmox_username
}

output "user_api_token" {
  description = "User API token (sensitive)"
  value       = data.external.user_token.result.token
  sensitive   = true
}

# Wyrm2 outputs
output "wyrm2" {
  description = "Wyrm2 VM info"
  value = {
    name           = module.wyrm2.vm_name
    id             = module.wyrm2.vm_id
    ipv4_addresses = module.wyrm2.ipv4_addresses
  }
}

output "instructions" {
  description = "Setup instructions and next steps"
  value       = <<-EOT

    ✅ Environment created successfully!

    Pool: ${proxmox_virtual_environment_pool.user_pool.pool_id}
    User: ${local.proxmox_username}

    VMs:
    - wyrm2 (ID: ${module.wyrm2.vm_id})

    Next steps:

    1. Wait for VM to boot (~30 seconds, no cloud-init rebuild needed)

    2. Get VM IP address:
       terraform output wyrm2

    3. SSH into the VM:
       ssh ${var.username}@<vm-ip>

    4. Access Proxmox web UI:
       URL: https://${var.proxmox_api_host}
       User: ${local.proxmox_username}
       Password: (set with: ssh root@${var.proxmox_host} "pveum user password ${local.proxmox_username}")

    To update VM config:
    - Rebuild image: nix build ./nix#wyrm2-image
    - Redeploy: terraform apply
    - Or from inside VM: sudo nixos-rebuild switch --flake github:agentydragon/ducktape?dir=nix&ref=devel#wyrm2
  EOT
}
