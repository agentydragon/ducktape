# LAYER 1: Infrastructure Provider Versions
# Hybrid cluster: Hetzner VPS + Proxmox home nodes
# Uses shared machine secrets from 00-persistent-auth

terraform {
  required_version = ">= 1.0"

  backend "local" {
    path = "terraform.tfstate"
  }

  required_providers {
    # Hetzner Cloud for VPS nodes
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
    # Proxmox for home nodes (Phase 2)
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.91.0"
    }
    # Talos Linux for all nodes
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.10.0"
    }
    # Kubernetes access
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38.0"
    }
    # Utility providers
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.1.0"
    }
  }
}
