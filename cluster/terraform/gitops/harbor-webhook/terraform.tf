terraform {
  required_version = ">= 1.0"

  required_providers {
    harbor = {
      source  = "goharbor/harbor"
      version = "~> 3.11"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.7.0"
    }
  }

  backend "kubernetes" {
    secret_suffix = "harbor-webhook"
    namespace     = "flux-system"
  }
}
