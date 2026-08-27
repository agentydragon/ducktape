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
      version = "~> 0.111.0" # Latest: v0.111.0
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.4.0" # Latest: v2.4.0
    }
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.11.0" # Latest: v0.11.0
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.2.0" # Latest: v3.2.0
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9.0" # Latest: v3.9.0
    }
    powerdns = {
      source  = "pan-net/powerdns"
      version = "~> 1.5.0" # DNS provider for PowerDNS
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.9.0" # Latest: v2.9.0 - Local file operations
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.3.0" # Latest: v3.3.0 - Null provider for triggers
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.3.0" # Latest: v4.3.0 - TLS certificate generation
    }
    flux = {
      source  = "fluxcd/flux"
      version = "~> 1.9.0" # Latest: v1.9.0 - FluxCD GitOps provider
    }
  }
}
