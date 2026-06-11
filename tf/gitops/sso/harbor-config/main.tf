terraform {
  required_version = ">= 1.0"

  required_providers {
    harbor = {
      source  = "goharbor/harbor"
      version = "~> 3.11"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38.0"
    }
  }

  backend "kubernetes" {
    secret_suffix = "harbor-oidc-config"
    namespace     = "flux-system"
  }
}

# Harbor admin password from ESO-synced K8s secret
data "kubernetes_secret" "harbor_admin_password" {
  metadata {
    name      = "harbor-admin-initial"
    namespace = "harbor"
  }
}

provider "harbor" {
  url      = var.harbor_url
  username = "admin"
  password = data.kubernetes_secret.harbor_admin_password.data["HARBOR_ADMIN_PASSWORD"]
}

# OIDC credentials from ESO-synced K8s secret (source: kv/sso/harbor in Vault)
data "kubernetes_secret" "harbor_oidc" {
  metadata {
    name      = "harbor-oauth-client-secret"
    namespace = "harbor"
  }
}

# Configure Harbor OIDC authentication with Authentik
resource "harbor_config_auth" "oidc" {
  auth_mode = "oidc_auth"

  oidc_name          = "Authentik"
  oidc_endpoint      = "${var.authentik_url}/application/o/harbor/"
  oidc_client_id     = data.kubernetes_secret.harbor_oidc.data["client_id"]
  oidc_client_secret = data.kubernetes_secret.harbor_oidc.data["client_secret"]
  oidc_scope         = "openid,email,profile"
  oidc_verify_cert   = true

  oidc_auto_onboard = true
  oidc_user_claim   = "preferred_username"
  oidc_groups_claim = "groups"
  oidc_admin_group  = "harbor-admins"
}
