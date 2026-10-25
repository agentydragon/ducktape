terraform {
  required_version = ">= 1.0"

  required_providers {
    forgejo = {
      source = "svalabs/forgejo"
    }
    gitlab = {
      source = "gitlabhq/gitlab"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
    random = {
      source = "hashicorp/random"
    }
  }
}
