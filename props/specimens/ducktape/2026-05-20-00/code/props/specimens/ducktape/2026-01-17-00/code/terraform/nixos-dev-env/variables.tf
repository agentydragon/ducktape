# Variables for NixOS Dev Environment
# Shared infrastructure + defaults for VM modules

# =============================================================================
# PROXMOX CONFIGURATION
# =============================================================================

variable "proxmox_host" {
  description = "Proxmox host for SSH access"
  type        = string
  default     = "atlas"
}

variable "proxmox_api_host" {
  description = "Proxmox API host FQDN"
  type        = string
  default     = "atlas.agentydragon.com"
}

variable "proxmox_node_name" {
  description = "Proxmox node name for VM deployment"
  type        = string
  default     = "atlas"
}

variable "storage" {
  description = "Storage location for VM disks"
  type        = string
  default     = "local-zfs"
}

variable "network_bridge" {
  description = "Network bridge for VMs"
  type        = string
  default     = "vmbr0"
}

# =============================================================================
# USER/POOL CONFIGURATION
# =============================================================================

variable "username" {
  description = "Username for VM user accounts"
  type        = string
  default     = "user"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.username))
    error_message = "Username must start with a letter and contain only lowercase letters, numbers, and hyphens."
  }
}

variable "proxmox_username" {
  description = "Username for Proxmox pool user (without @pve, defaults to username)"
  type        = string
  default     = ""
  validation {
    condition     = var.proxmox_username == "" || can(regex("^[a-z][a-z0-9-]*$", var.proxmox_username))
    error_message = "Proxmox username must start with a letter and contain only lowercase letters, numbers, and hyphens."
  }
}

variable "pool_name" {
  description = "Resource pool name (defaults to pool-{proxmox_username})"
  type        = string
  default     = ""
}

variable "user_comment" {
  description = "Comment for Proxmox user"
  type        = string
  default     = "Managed by Terraform"
}

# =============================================================================
# NIXOS/HOME-MANAGER FLAKE CONFIGURATION
# =============================================================================

variable "ssh_public_key" {
  description = "SSH public key (auto-detected from ~/.ssh if not specified)"
  type        = string
  default     = ""
}

variable "nixos_flake_url" {
  description = "Flake URL for NixOS system configuration"
  type        = string
  default     = "github:agentydragon/ducktape?dir=nix/nixos&ref=devel"
}

variable "home_manager_flake_url" {
  description = "Flake URL for home-manager configuration"
  type        = string
  default     = "github:agentydragon/ducktape?dir=nix/home&ref=devel"
}

variable "home_manager_host" {
  description = "Home-manager host config name from ducktape flake"
  type        = string
  default     = "nixos-vm"
}

# =============================================================================
# ENVIRONMENT VARIABLES
# =============================================================================

variable "custom_env_vars" {
  description = "Custom environment variables to inject into VMs"
  type        = map(string)
  default     = {}
  sensitive   = true
}

variable "openai_api_key" {
  description = "OpenAI API key to inject into VMs"
  type        = string
  default     = ""
  sensitive   = true
}

variable "anthropic_api_key" {
  description = "Anthropic API key to inject into VMs"
  type        = string
  default     = ""
  sensitive   = true
}
