terraform {
  required_version = ">= 1.0"

  required_providers {
    forgejo = {
      source  = "svalabs/forgejo"
      version = "~> 1.5"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
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

  # Placeholder; the actual backend (pg, tofu-state) is injected by the Terraform
  # CR's backendConfig.customConfiguration (see cluster/k8s/forgejo/haku-state).
  backend "kubernetes" {
    secret_suffix = "haku-state"
    namespace     = "flux-system"
  }
}
