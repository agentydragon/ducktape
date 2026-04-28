terraform {
  required_version = ">= 1.0"

  required_providers {
    authentik = {
      source  = "goauthentik/authentik"
      version = "~> 2026.2"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
  }

  backend "kubernetes" {
    secret_suffix = "gatus-sso"
    namespace     = "flux-system"
  }
}

data "kubernetes_secret" "authentik_bootstrap" {
  metadata {
    name      = "authentik-bootstrap"
    namespace = "authentik"
  }
}

provider "authentik" {
  url   = "http://authentik-server.authentik.svc.cluster.local"
  token = data.kubernetes_secret.authentik_bootstrap.data["AUTHENTIK_BOOTSTRAP_TOKEN"]
}

# --- Flows and signing key (looked up, not managed) ---

data "authentik_flow" "implicit_consent" {
  slug = "default-provider-authorization-implicit-consent"
}

data "authentik_flow" "invalidation" {
  slug = "default-provider-invalidation-flow"
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

data "authentik_group" "admins" {
  name = "authentik Admins"
}

# --- OAuth2 Provider ---
# client_secret omitted — Authentik auto-generates it.

resource "authentik_provider_oauth2" "gatus" {
  name               = "gatus"
  client_id          = "gatus"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://status.allegedly.works/authorization-code/callback"
    },
  ]
}

# --- Application ---

resource "authentik_application" "gatus" {
  name              = "Gatus"
  slug              = "gatus"
  protocol_provider = authentik_provider_oauth2.gatus.id
  meta_description  = "Gatus Health Monitoring"
  meta_icon         = "https://cdn.simpleicons.org/statuspage"
  meta_launch_url   = "https://status.allegedly.works"
  open_in_new_tab   = true
}

# --- Access Policy ---

resource "authentik_policy_binding" "gatus_admins" {
  target = authentik_application.gatus.uuid # .id is slug, not UUID; API requires UUID
  group  = data.authentik_group.admins.id
  order  = 0
}

# --- K8s Secret for Gatus OIDC config ---

resource "kubernetes_secret" "gatus_oidc" {
  metadata {
    name      = "gatus-oidc-secret"
    namespace = "gatus"
  }

  data = {
    GATUS_CLIENT_SECRET = authentik_provider_oauth2.gatus.client_secret
  }
}
