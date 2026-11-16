variable "flake_target" {
  description = "Nix flake package name (e.g., 'wyrm2' builds nix#wyrm2-image)"
  type        = string
}

variable "proxmox_host" {
  description = "Proxmox host address for SCP upload"
  type        = string
}

variable "repo_root" {
  description = "Path to the repository root (for nix build)"
  type        = string
}

variable "build_enabled" {
  description = "When false, skip image build/upload (image assumed to already exist on Proxmox)"
  type        = bool
  default     = true
}
