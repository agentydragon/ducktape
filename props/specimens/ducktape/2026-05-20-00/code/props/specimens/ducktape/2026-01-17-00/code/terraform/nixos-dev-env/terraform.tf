# Unified Proxmox User + NixOS VM Environment
# Creates an isolated user with resource pool and provisions a NixOS VM

terraform {
  required_version = ">= 1.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.86.0"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.0"
    }
  }
}
