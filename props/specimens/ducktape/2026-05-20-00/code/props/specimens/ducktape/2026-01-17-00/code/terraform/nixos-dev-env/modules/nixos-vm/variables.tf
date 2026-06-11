# NixOS VM Module Variables

variable "vm_name" {
  description = "Name of the VM"
  type        = string
}

variable "vm_id" {
  description = "VM ID in Proxmox (leave null for auto-assignment)"
  type        = number
  default     = null
}

variable "username" {
  description = "Username for VM user account"
  type        = string
  default     = "user"
}

variable "vcpus" {
  description = "Number of vCPUs"
  type        = number
  default     = 4
}

variable "memory_mb" {
  description = "Memory in MB"
  type        = number
  default     = 8192
}

variable "disk_size_gb" {
  description = "Disk size in GB"
  type        = number
  default     = 50
}

variable "auto_start" {
  description = "Start VM after creation"
  type        = bool
  default     = true
}

# NixOS flake configuration
variable "nixos_flake_url" {
  description = "Flake URL for NixOS configuration"
  type        = string
}

variable "nixos_host" {
  description = "NixOS host config name from flake"
  type        = string
}

# Home-manager flake configuration
variable "home_manager_flake_url" {
  description = "Flake URL for home-manager configuration"
  type        = string
}

variable "home_manager_host" {
  description = "Home-manager host config name"
  type        = string
}

variable "rebuild_trigger" {
  description = "Change this value to force a rebuild (e.g., timestamp)"
  type        = string
  default     = ""
}

# Passed from parent (infrastructure context)
variable "proxmox_node_name" {
  description = "Proxmox node name"
  type        = string
}

variable "storage" {
  description = "Storage location for VM disk"
  type        = string
}

variable "network_bridge" {
  description = "Network bridge for VM"
  type        = string
}

variable "pool_id" {
  description = "Proxmox pool ID to place VM in"
  type        = string
}

variable "ssh_public_key" {
  description = "SSH public key for VM access"
  type        = string
}
