# SSO OAuth2 providers managed by Terraform.
#
# Replaces blueprint+Vault+ESO chain for Grafana, Headlamp, and
# OpenClaw-Agent. TF creates the Authentik provider (which owns the
# client_secret), then writes K8s secrets into the authentik namespace.
# Reflector mirrors them to consumer namespaces.

terraform {
  required_version = ">= 1.0"

  required_providers {
    authentik = {
      source  = "goauthentik/authentik"
      version = "~> 2025.10"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  backend "kubernetes" {
    secret_suffix = "sso-providers"
    namespace     = "flux-system"
  }
}

data "kubernetes_secret" "authentik_bootstrap" {
  metadata {
    name      = "authentik-bootstrap"
    namespace = "authentik"
  }
}

data "kubernetes_secret" "authentik_user_password" {
  metadata {
    name      = "authentik-user-password"
    namespace = "authentik"
  }
}

data "kubernetes_secret" "auragon_google_email" {
  metadata {
    name      = "authentik-auragon-google-email"
    namespace = "authentik"
  }
}

provider "authentik" {
  url   = var.authentik_url_override != "" ? var.authentik_url_override : "http://authentik-server.authentik.svc.cluster.local"
  token = data.kubernetes_secret.authentik_bootstrap.data["AUTHENTIK_BOOTSTRAP_TOKEN"]
}

# --- Shared data sources ---

data "authentik_flow" "implicit_consent" {
  slug = "default-provider-authorization-implicit-consent"
}

data "authentik_flow" "invalidation" {
  slug = "default-provider-invalidation-flow"
}

data "authentik_group" "admins" {
  name = "authentik Admins"
}

data "authentik_user" "akadmin" {
  username = "akadmin"
}

resource "authentik_user" "agentydragon" {
  username = "agentydragon"
  name     = "Rai"
  email    = "agentydragon@gmail.com"
  password = data.kubernetes_secret.authentik_user_password.data["USER_PASSWORD"]
}

# Google OAuth-only user. Email is SOPS-encrypted — sourced from
# authentik-auragon-google-email secret so it stays out of plaintext git.
# Matches via user_matching_mode = "email_link" on authentik_source_oauth.google.
resource "authentik_user" "auragon" {
  username = "auragon"
  name     = "auragon"
  email    = data.kubernetes_secret.auragon_google_email.data["AURAGON_GOOGLE_EMAIL"]
}

resource "authentik_group" "authentik_admins" {
  name         = "authentik Admins"
  is_superuser = true
  users        = [data.authentik_user.akadmin.pk, tonumber(authentik_user.agentydragon.id)]
}

resource "authentik_group" "grafana_admins" {
  name  = "Grafana Admins"
  users = [tonumber(authentik_user.agentydragon.id)]
}

resource "authentik_group" "study_casino" {
  name = "study-casino"
  users = [
    tonumber(authentik_user.agentydragon.id),
    tonumber(authentik_user.auragon.id),
  ]
}

data "authentik_certificate_key_pair" "self_signed" {
  name = "authentik Self-signed Certificate"
}

data "authentik_property_mapping_provider_scope" "openid" {
  managed = "goauthentik.io/providers/oauth2/scope-openid"
}

data "authentik_property_mapping_provider_scope" "email" {
  managed = "goauthentik.io/providers/oauth2/scope-email"
}

data "authentik_property_mapping_provider_scope" "profile" {
  managed = "goauthentik.io/providers/oauth2/scope-profile"
}

# Custom airlock scope mappings (defined in airlock-scope-mappings.yaml blueprint)
data "authentik_property_mapping_provider_scope" "propose" {
  scope_name = "propose"
}

data "authentik_property_mapping_provider_scope" "read" {
  scope_name = "read"
}
