terraform {
  required_version = ">= 1.0"

  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "~> 3.22.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
  }

  backend "kubernetes" {
    secret_suffix = "grafana-flux-token"
    namespace     = "flux-system"
  }
}

data "kubernetes_secret" "grafana_admin" {
  metadata {
    name      = "grafana-admin-password"
    namespace = "monitoring"
  }
}

provider "grafana" {
  url  = var.grafana_url
  auth = "admin:${data.kubernetes_secret.grafana_admin.data["admin-password"]}"
}

resource "grafana_service_account" "flux" {
  name = "flux-notifications"
  role = "Editor"
}

resource "grafana_service_account_token" "flux" {
  name               = "flux-token"
  service_account_id = grafana_service_account.flux.id
}

resource "kubernetes_secret" "grafana_flux_token" {
  metadata {
    name      = "grafana-flux-token"
    namespace = "flux-system"
  }

  data = {
    token = grafana_service_account_token.flux.key
  }
}
