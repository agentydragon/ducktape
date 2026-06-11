# NixOS VM Module Outputs

output "vm_id" {
  description = "The VM ID"
  value       = proxmox_virtual_environment_vm.vm.vm_id
}

output "vm_name" {
  description = "The VM name"
  value       = proxmox_virtual_environment_vm.vm.name
}

output "ipv4_addresses" {
  description = "The IPv4 addresses of the VM"
  value       = proxmox_virtual_environment_vm.vm.ipv4_addresses
}
