output "kubeconfig" {
  description = "Generated kubeconfig for cluster access (patched with real endpoint)"
  value = replace(
    talos_cluster_kubeconfig.cluster.kubeconfig_raw,
    "https://localhost:7445",
    local.kubeconfig_cluster_endpoint
  )
  sensitive = true
}

output "talos_config" {
  description = "Talos client configuration"
  value       = data.talos_client_configuration.cluster.talos_config
  sensitive   = true
}

output "home_bare_metal_iso_url" {
  description = "Talos metal ISO shared by home bare-metal workers and OVH nodes"
  value       = data.talos_image_factory_urls.kimsufi.urls.iso
}

output "optiplex_worker_machine_configuration" {
  description = "Generated Talos worker configuration for the home OptiPlex"
  value       = data.talos_machine_configuration.home_worker["optiplex"].machine_configuration
  sensitive   = true
}

output "wyrm2" {
  description = "Wyrm2 VM info"
  value = {
    name           = proxmox_virtual_environment_vm.wyrm2.name
    id             = proxmox_virtual_environment_vm.wyrm2.vm_id
    ipv4_addresses = proxmox_virtual_environment_vm.wyrm2.ipv4_addresses
  }
}

output "instructions" {
  description = "Setup instructions and next steps"
  value       = <<-EOT

    VM: wyrm2 (ID: ${proxmox_virtual_environment_vm.wyrm2.vm_id})

    Workflows:

    Initial provisioning (build bootstrap image + deploy full config):
      tofu apply -var="rebuild_image=true" -var="nixos_rebuild=true"

    VM hardware changes only (CPU, RAM, disks):
      tofu apply

    Deploy NixOS config changes:
      tofu apply -var="nixos_rebuild=true"
      # or manually:
      ssh wyrm2 'sudo nixos-rebuild switch --flake github:agentydragon/ducktape?ref=devel#wyrm2'

    After deploying, approve the kubelet CSR to join the cluster:
      kubectl get csr
      kubectl certificate approve <csr-name>
  EOT
}

# TALOS MACHINE SECRETS (read by cluster/bootstrap.py, which derives the NixOS worker join
# material from it)

output "machine_secrets_json" {
  description = "Full machine secrets as JSON (for SOPS backup)"
  value       = jsonencode(talos_machine_secrets.cluster.machine_secrets)
  sensitive   = true
}
