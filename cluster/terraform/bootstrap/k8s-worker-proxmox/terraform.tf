# K8s Worker (Proxmox) — Provider Versions

terraform {
  required_version = ">= 1.0"

  backend "local" {
    path = "terraform.tfstate"
  }

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.0"
    }
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.91.0"
    }
  }
}
