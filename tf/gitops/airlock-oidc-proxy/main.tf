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
    secret_suffix = "airlock-oidc-proxy"
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

# Custom airlock scope mappings (created by openclaw-agent-oauth2 blueprint)
data "authentik_property_mapping_provider_scope" "propose" {
  name = "airlock: propose"
}

data "authentik_property_mapping_provider_scope" "read" {
  name = "airlock: read"
}

data "authentik_group" "admins" {
  name = "authentik Admins"
}

# --- OAuth2 Provider ---
# Confidential client for OIDCProxy to proxy auth flows to Authentik.
# client_secret omitted — Authentik auto-generates it.

resource "authentik_provider_oauth2" "airlock_oidc_proxy" {
  name               = "airlock-oidc-proxy"
  client_id          = "airlock-oidc-proxy"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.propose.id,
    data.authentik_property_mapping_provider_scope.read.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://airlock.allegedly.works/mcp/auth/callback"
    },
  ]
}

# --- Application ---

resource "authentik_application" "airlock_oidc_proxy" {
  name              = "Airlock OIDCProxy"
  slug              = "airlock-oidc-proxy"
  protocol_provider = authentik_provider_oauth2.airlock_oidc_proxy.id
  meta_description  = "OIDCProxy upstream auth for MCP clients connecting to airlock"
}

# --- Access Policy ---

resource "authentik_policy_binding" "airlock_oidc_proxy_admins" {
  target = authentik_application.airlock_oidc_proxy.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# --- K8s Secret for airlock OIDCProxy credentials ---

resource "kubernetes_secret" "airlock_oidc_proxy" {
  metadata {
    name      = "airlock-oidc-proxy-credentials"
    namespace = "airlock"
  }

  data = {
    client_id     = authentik_provider_oauth2.airlock_oidc_proxy.client_id
    client_secret = authentik_provider_oauth2.airlock_oidc_proxy.client_secret
  }
}
