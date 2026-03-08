# Libvirt VM Module Outputs

output "vm_name" {
  description = "The VM name"
  value       = libvirt_domain.vm.name
}

output "ip_addresses" {
  description = "The IP addresses of the VM (from DHCP lease)"
  value       = libvirt_domain.vm.network_interface[*].addresses
}
