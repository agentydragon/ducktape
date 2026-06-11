# SHARED TERRAFORM CONFIGURATION - SINGLE SOURCE OF TRUTH
# This file is symlinked/copied to all terraform directories
# Updated with latest versions as of November 2025

terraform {
  required_version = ">= 1.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38.0" # Latest: v2.38.0
    }
    authentik = {
      source  = "goauthentik/authentik"
      version = "~> 2025.10.0" # Latest: v2025.10.0
    }
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.86.0" # Latest: v0.86.0 (major improvement from 0.70)
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3.0" # Latest: v2.3.5
    }
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.9.0" # Latest: v0.9.0 (major version bump)
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.1.0" # Latest: v3.1.0 (major version bump from 2.x)
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0" # Latest: v3.7.2
    }
    harbor = {
      source  = "goharbor/harbor"
      version = "~> 3.11.0" # Latest: v3.11.2
    }
    gitea = {
      source  = "go-gitea/gitea"
      version = "~> 0.7.0" # Latest: v0.7.0 (major improvement from 0.5)
    }
    powerdns = {
      source  = "pan-net/powerdns"
      version = "~> 1.5.0" # DNS provider for PowerDNS
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5.0" # Latest: v2.5.3 - Local file operations
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.0" # Latest: v3.2.4 - Null provider for triggers
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.1.0" # Latest: v4.1.0 - TLS certificate generation
    }
    flux = {
      source  = "fluxcd/flux"
      version = "~> 1.7.0" # Latest: v1.7.4 - FluxCD GitOps provider
    }
  }
}
