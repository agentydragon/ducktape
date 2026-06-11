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

    📋 Next steps:

    1. Wait for VMs to boot and cloud-init to complete (~2-3 minutes)

    2. Get VM IP addresses:
       terraform output wyrm2

    3. SSH into a VM (passwordless):
       ssh ${var.username}@<vm-ip>

    4. Check home-manager status:
       ssh ${var.username}@<vm-ip> 'home-manager generations'

    5. Access Proxmox web UI as the user:
       URL: https://${var.proxmox_api_host}:8006
       User: ${local.proxmox_username}
       Password: (set with: ssh root@${var.proxmox_host} "pveum user password ${local.proxmox_username}")

    Configuration:
    - NixOS flake: ${var.nixos_flake_url}
    - Home-manager flake: ${var.home_manager_flake_url}#${var.home_manager_host}

    To update VM config after changes:
    - Push to devel branch, then: terraform apply
    - Or manually: ssh user@<ip> 'sudo nixos-rebuild switch --flake ${var.nixos_flake_url}#wyrm2'

    🔐 Environment variables baked into VMs:
    - Proxmox: PROXMOX_VE_ENDPOINT, PROXMOX_VE_USERNAME, PROXMOX_VE_API_TOKEN, PROXMOX_POOL_ID
    - LLM API keys: OPENAI_API_KEY, ANTHROPIC_API_KEY (if provided via ./apply.sh)
  EOT
}
