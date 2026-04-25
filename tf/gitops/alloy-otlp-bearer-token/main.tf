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
  }

  backend "kubernetes" {
    secret_suffix = "alloy-otlp-bearer-token"
    namespace     = "flux-system"
  }
}

# Read the Authentik bootstrap token from the K8s Secret (populated by ESO from Vault).
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

data "authentik_flow" "authentication" {
  slug = "default-authentication-flow"
}

data "authentik_flow" "implicit_consent" {
  slug = "default-provider-authorization-implicit-consent"
}

data "authentik_flow" "invalidation" {
  slug = "default-provider-invalidation-flow"
}

# External OTLP/HTTP ingestion endpoint used by Claude Code hooks to send traces
# to Grafana Alloy, with Authentik proxy outpost validating Bearer tokens.

resource "authentik_provider_proxy" "alloy_otlp" {
  name                  = "alloy-otlp"
  external_host         = "https://alloy-otlp.allegedly.works"
  internal_host         = "http://alloy.monitoring.svc.cluster.local:4318"
  mode                  = "proxy"
  authentication_flow   = data.authentik_flow.authentication.id
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  access_token_validity = "hours=24"
}

resource "authentik_application" "alloy_otlp" {
  name              = "Alloy OTLP"
  slug              = "alloy-otlp"
  protocol_provider = authentik_provider_proxy.alloy_otlp.id
  meta_description  = "Grafana Alloy OTLP ingestion endpoint for external clients (Claude hooks, etc.)"
  open_in_new_tab   = false
}

# Service account for machine-to-machine Bearer token auth. Authentik proxy
# outposts validate `Authorization: Bearer <token>` by looking up the token in
# Authentik's API and checking the owning user has application access.
resource "authentik_user" "alloy_otlp_sa" {
  username = "alloy-otlp-service-account"
  name     = "Alloy OTLP Service Account"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

resource "authentik_token" "alloy_otlp" {
  identifier   = "alloy-otlp-api-key"
  user         = authentik_user.alloy_otlp_sa.id
  intent       = "api"
  expiring     = false
  retrieve_key = true
  description  = "Bearer token for Claude hooks → Alloy OTLP ingestion"
}

# TODO: rotate this token periodically the way claude-jwt-rotation rotates the
# k8s JWT. Different mechanism — this is an Authentik *API token* (validated
# by the proxy outpost via Authentik API lookup), not an OIDC JWT
# (cryptographically signed, validated via JWKS), so the rotate.sh
# `client_credentials` exchange in cluster/k8s/agents/claude-jwt-rotation/
# doesn't apply directly. A rotator here would either (a) call Authentik's
# admin API to delete + recreate the token periodically, or (b) migrate the
# OTLP ingestion path off proxy-outpost auth onto OIDC JWT validation so the
# same client_credentials script can be reused.

resource "authentik_policy_binding" "alloy_otlp_sa" {
  target = authentik_application.alloy_otlp.uuid
  user   = authentik_user.alloy_otlp_sa.id
  order  = 0
}

# Canonical K8s Secret holding the Authentik-generated bearer token. Clients
# (Claude Code hook daemon, CLI env scripts) read this via kubectl and export
# it as DUCKTAPE_OTEL_BEARER_TOKEN. Reflector annotations allow any namespace
# that needs the token to mirror it in.
resource "kubernetes_secret" "alloy_otlp_bearer_token" {
  metadata {
    name      = "alloy-otlp-bearer-token"
    namespace = "claude-sandbox"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = ""
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = ""
    }
  }

  data = {
    token = authentik_token.alloy_otlp.key
  }
}
