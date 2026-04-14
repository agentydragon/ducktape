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
    secret_suffix = "agent-machine-access"
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

# --- Shared data sources ---

data "authentik_flow" "authentication" {
  slug = "default-authentication-flow"
}

data "authentik_flow" "implicit_consent" {
  slug = "default-provider-authorization-implicit-consent"
}

data "authentik_flow" "invalidation" {
  slug = "default-provider-invalidation-flow"
}

data "authentik_group" "admins" {
  name = "authentik Admins"
}

# Signing key + OIDC scope property mappings for the grocy-mcp user-login
# OAuth2 provider below. Mirrors
# <../authentik-mcp-poc/main.tf>'s data-source block.
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

# --- Service Account ---
# Shared service account for Claude/OpenClaw sandbox agents.
# Used for Authentik API access (e.g., querying user info).

resource "authentik_user" "agent_sa" {
  username = "agent-service-account"
  name     = "Agent Service Account"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

resource "authentik_token" "agent_sa_token" {
  identifier   = "agent-api-key"
  user         = authentik_user.agent_sa.id
  intent       = "api"
  expiring     = false
  retrieve_key = true
  description  = "Bearer token for Claude/OpenClaw sandbox agents"
}

# K8s secret in claude-sandbox with Reflector annotations for openclaw-sandbox.
resource "kubernetes_secret" "agent_bearer_token" {
  metadata {
    name      = "agent-bearer-token"
    namespace = "claude-sandbox"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "openclaw-sandbox"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "openclaw-sandbox"
    }
  }

  data = {
    token = authentik_token.agent_sa_token.key
  }
}

# --- Grocy (proxy provider + machine auth via M2M app password) ---
# Replaces the grocy-sso.yaml blueprint. TF is the sole manager.
# Humans authenticate via browser session through the proxy outpost.
# Agents authenticate via Authentik M2M: POST to /application/o/token/
# with grant_type=client_credentials, client_id, username, password
# (app_password) → JWT → Bearer token against grocy.allegedly.works.
# The proxy outpost only accepts JWTs from its own provider.

resource "authentik_provider_proxy" "grocy" {
  name                  = "grocy"
  external_host         = "https://grocy.allegedly.works"
  internal_host         = "http://grocy.grocy.svc.cluster.local:80"
  mode                  = "proxy"
  authentication_flow   = data.authentik_flow.authentication.id
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  access_token_validity = "hours=24"

  # Lets the grocy-mcp server's tool handlers mint Grocy-scoped JWTs on
  # behalf of the calling MCP user, via the RFC 7521 jwt-bearer client-
  # credentials path. See x/authentik_mcp_poc/NOTES.md §4-§5 for the full
  # trace through Authentik's __validate_jwt_from_provider.
  jwt_federation_providers = [authentik_provider_oauth2.grocy_mcp.id]
}

resource "authentik_application" "grocy" {
  name              = "Grocy"
  slug              = "grocy"
  protocol_provider = authentik_provider_proxy.grocy.id
  meta_description  = "Groceries & household management"
  meta_icon         = "https://cdn.simpleicons.org/grocy"
  meta_launch_url   = "https://grocy.allegedly.works"
  open_in_new_tab   = true
}

# Human access: admins group.
resource "authentik_policy_binding" "grocy_admins" {
  target = authentik_application.grocy.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# Machine access: service account for agent M2M auth via app password.
resource "authentik_user" "grocy_machine_sa" {
  username = "grocy-machine"
  name     = "Grocy Machine Agent"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

resource "authentik_token" "grocy_machine_app_password" {
  identifier   = "grocy-machine-app-password"
  user         = authentik_user.grocy_machine_sa.id
  intent       = "app_password"
  expiring     = true
  retrieve_key = true
  description  = "App password for Grocy machine auth (M2M via proxy provider)"
}

resource "authentik_policy_binding" "grocy_machine_sa" {
  target = authentik_application.grocy.uuid
  user   = authentik_user.grocy_machine_sa.id
  order  = 10
}

# K8s secret with M2M credentials for agents to authenticate to Grocy.
#
# Authentik's M2M flow for proxy providers authenticates with a service-account
# user's username + app_password. Envoy's envoy.filters.http.credential_injector
# filter only speaks the standard OAuth2 client_credentials shape
# (client_id + client_secret), so we encode the credential as
# client_secret_b64 = base64("<username>:<app_password>") — Authentik accepts
# this as an equivalent form. The grocy-mcp Envoy sidecar consumes these two
# fields directly via secretKeyRef.
resource "kubernetes_secret" "grocy_machine_credentials" {
  metadata {
    name      = "grocy-machine-credentials"
    namespace = "claude-sandbox"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "openclaw-sandbox,grocy-mcp"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "openclaw-sandbox,grocy-mcp"
    }
  }

  data = {
    client_id         = authentik_provider_proxy.grocy.client_id
    client_secret_b64 = base64encode("${authentik_user.grocy_machine_sa.username}:${authentik_token.grocy_machine_app_password.key}")
  }
}

# Flux postBuild.substituteFrom variables for the grocy-mcp Kustomization's
# Envoy sidecar config. Flux resolves substituteFrom references in the same
# namespace as the Kustomization itself, which lives in flux-system — so the
# substitution source has to live there too.
#
# Only client_id needs build-time substitution: it's a plain string field
# in Envoy's OAuth2 filter config and has no DataSource variant. client_id
# isn't sensitive (it's a public OAuth client identifier), so a plain
# ConfigMap is appropriate — same pattern as cert-manager-issuer-config.
#
# The actual secret (client_secret_b64) is read by Envoy at startup from
# an env var populated via secretKeyRef on the Envoy container
# (DataSource.environment_variable), so it never passes through flux-system
# or the rendered bootstrap file.
#
# Keys are upper-case because Flux substituteFrom substitutes by raw key
# name into `${KEY}` placeholders in the built manifests.
resource "kubernetes_config_map" "grocy_envoy_vars" {
  metadata {
    name      = "grocy-envoy-vars"
    namespace = "flux-system"
  }

  data = {
    CLIENT_ID = authentik_provider_proxy.grocy.client_id
  }
}

# --- Grocy MCP: OAuth2 provider for user-login (OIDCProxy upstream) ---
#
# Wiring for x/grocy_mcp — an auth-aware MCP server that drives Grocy's
# REST API on behalf of the calling user. The server uses FastMCP's
# OIDCProxy, which requires a dedicated confidential OAuth2 client so
# claude.ai / Claude Code can drive OAuth 2.1 + PKCE + RFC 7591 DCR
# against it. The existing `grocy` proxy provider above cannot be reused
# for this (its OAuth2 client is locked to the outpost's internal
# callback URL). The new provider is then listed in the proxy provider's
# `jwt_federation_providers` so the tool handlers can exchange the
# caller's upstream token for a Grocy-scoped one via the RFC 7521
# jwt-bearer client-credentials path — see
# <x/authentik_mcp_poc/NOTES.md> §4-§6 for why each piece is
# load-bearing.

resource "authentik_provider_oauth2" "grocy_mcp" {
  name               = "grocy-mcp"
  client_id          = "grocy-mcp"
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
      # OIDCProxy's redirect path defaults to "/auth/callback" relative
      # to its `base_url`. x/grocy_mcp/server.py sets base_url to the
      # bare public URL (no /mcp) — see the `_build_auth` comment for
      # why.
      url = "https://grocy-mcp.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "grocy_mcp" {
  name              = "Grocy MCP"
  slug              = "grocy-mcp"
  protocol_provider = authentik_provider_oauth2.grocy_mcp.id
  meta_description  = "Auth-aware MCP server for Grocy — OIDCProxy upstream for user login"
  meta_launch_url   = "https://grocy-mcp.allegedly.works"
}

resource "authentik_policy_binding" "grocy_mcp_admins" {
  target = authentik_application.grocy_mcp.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# K8s secret with OIDC client credentials and the grocy proxy provider's
# auto-generated client_id that the server uses as the `client_id` in its
# RFC 7521 jwt-bearer token exchange. Namespace ownership: the
# `grocy-mcp-oidc-namespace` Flux Kustomization creates the namespace
# first; `agent-machine-access-tf` depends on it so TF runs after the
# namespace exists.
resource "kubernetes_secret" "grocy_mcp_oidc" {
  metadata {
    name      = "grocy-mcp-oidc"
    namespace = "grocy-mcp-oidc"
  }

  data = {
    client_id             = authentik_provider_oauth2.grocy_mcp.client_id
    client_secret         = authentik_provider_oauth2.grocy_mcp.client_secret
    grocy_proxy_client_id = authentik_provider_proxy.grocy.client_id
  }
}
