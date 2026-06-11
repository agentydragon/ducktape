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
  }

  backend "kubernetes" {
    secret_suffix = "harbor-proxy-cache"
    namespace     = "flux-system"
  }
}
