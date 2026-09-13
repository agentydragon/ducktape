terraform {
  required_version = ">= 1.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
  }

  backend "kubernetes" {
    secret_suffix = "gaffer-private-ghcr-pull"
    namespace     = "flux-system"
  }
}
