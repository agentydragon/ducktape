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
# Authentik's M2M flow for proxy providers has several accepted encodings of
# username+app_password. We expose two of them so different proxy sidecars can
# pick whichever matches their config surface:
#
#   1. username + app_password (form params)
#      Used by the Python auth proxy in grocy/auth_proxy/, which posts them
#      verbatim via authlib.
#
#   2. client_secret_b64 = base64("<username>:<app_password>")
#      Used by Envoy's envoy.filters.http.credential_injector filter, which
#      only supports the standard OAuth2 client_credentials shape
#      (client_id + client_secret). Authentik accepts this base64 encoding as
#      an alternative representation of the same M2M credential.
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
    username          = authentik_user.grocy_machine_sa.username
    app_password      = authentik_token.grocy_machine_app_password.key
    client_secret_b64 = base64encode("${authentik_user.grocy_machine_sa.username}:${authentik_token.grocy_machine_app_password.key}")
    token_url         = "https://auth.allegedly.works/application/o/token/"
    upstream_url      = "https://grocy.allegedly.works"
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
