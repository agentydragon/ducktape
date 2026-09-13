terraform {
  required_version = ">= 1.0"

  required_providers {
    forgejo = {
      source  = "svalabs/forgejo"
      version = "~> 1.5"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  backend "kubernetes" {
    secret_suffix = "forgejo-props"
    namespace     = "flux-system"
  }
}
