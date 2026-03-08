output "import_path" {
  description = "Proxmox import path for the built image (e.g., 'local:import/wyrm2.qcow2')"
  value       = "local:import/${var.flake_target}.qcow2"
}
