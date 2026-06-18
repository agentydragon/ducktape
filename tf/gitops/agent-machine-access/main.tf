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
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
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

data "authentik_property_mapping_provider_scope" "offline_access" {
  managed = "goauthentik.io/providers/oauth2/scope-offline_access"
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

# --- Grocy SF household (proxy provider + MCP OAuth2) ---

resource "authentik_group" "grocy_sf_household" {
  name = "SF household"
  users = [
    data.authentik_user.agentydragon.pk,
    data.authentik_user.auragon.pk,
  ]
}

resource "authentik_provider_proxy" "grocy_sf" {
  name                  = "grocy-sf"
  external_host         = "https://grocy-sf.allegedly.works"
  internal_host         = "http://grocy.grocy-sf.svc.cluster.local:80"
  mode                  = "proxy"
  authentication_flow   = data.authentik_flow.authentication.id
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  access_token_validity = "hours=24"

  jwt_federation_providers = [authentik_provider_oauth2.grocy_mcp_sf.id]
}

resource "authentik_application" "grocy_sf" {
  name              = "Grocy SF"
  slug              = "grocy-sf"
  protocol_provider = authentik_provider_proxy.grocy_sf.id
  meta_description  = "Groceries & household management (SF)"
  meta_icon         = "https://cdn.simpleicons.org/grocy"
  meta_launch_url   = "https://grocy-sf.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "grocy_sf_household" {
  target = authentik_application.grocy_sf.uuid
  group  = authentik_group.grocy_sf_household.id
  order  = 0
}

moved {
  from = authentik_policy_binding.grocy_sf_admins
  to   = authentik_policy_binding.grocy_sf_household
}

resource "authentik_provider_oauth2" "grocy_mcp_sf" {
  name               = "grocy-mcp-sf"
  client_id          = "grocy-mcp-sf"
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
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://grocy-mcp-sf.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "grocy_mcp_sf" {
  name              = "Grocy MCP SF"
  slug              = "grocy-mcp-sf"
  protocol_provider = authentik_provider_oauth2.grocy_mcp_sf.id
  meta_description  = "Auth-aware MCP server for Grocy SF household"
  meta_launch_url   = "https://grocy-mcp-sf.allegedly.works"
}

resource "authentik_policy_binding" "grocy_mcp_sf_household" {
  target = authentik_application.grocy_mcp_sf.uuid
  group  = authentik_group.grocy_sf_household.id
  order  = 0
}

moved {
  from = authentik_policy_binding.grocy_mcp_sf_admins
  to   = authentik_policy_binding.grocy_mcp_sf_household
}

resource "kubernetes_secret" "grocy_mcp_oidc_sf" {
  metadata {
    name      = "grocy-mcp-oidc-sf"
    namespace = "grocy-sf"
  }

  data = {
    client_id             = authentik_provider_oauth2.grocy_mcp_sf.client_id
    client_secret         = authentik_provider_oauth2.grocy_mcp_sf.client_secret
    grocy_proxy_client_id = authentik_provider_proxy.grocy_sf.client_id
  }
}

# --- Grocy Vallejo household (proxy provider + MCP OAuth2) ---

# Membership group gating both the user-facing Grocy Vallejo webapp and its
# MCP. Replaces the previous "authentik Admins" binding so non-admins can be
# granted household access without elevating them to platform admins.
resource "authentik_group" "grocy_vallejo_household" {
  name = "grocy-vallejo-household"
  users = [
    data.authentik_user.agentydragon.pk,
    data.authentik_user.auragon.pk,
  ]
}

resource "authentik_provider_proxy" "grocy_vallejo" {
  name                  = "grocy-vallejo"
  external_host         = "https://grocy-vallejo.allegedly.works"
  internal_host         = "http://grocy.grocy-vallejo.svc.cluster.local:80"
  mode                  = "proxy"
  authentication_flow   = data.authentik_flow.authentication.id
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  access_token_validity = "hours=24"

  jwt_federation_providers = [authentik_provider_oauth2.grocy_mcp_vallejo.id]
}

resource "authentik_application" "grocy_vallejo" {
  name              = "Grocy Vallejo"
  slug              = "grocy-vallejo"
  protocol_provider = authentik_provider_proxy.grocy_vallejo.id
  meta_description  = "Groceries & household management (Vallejo)"
  meta_icon         = "https://cdn.simpleicons.org/grocy"
  meta_launch_url   = "https://grocy-vallejo.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "grocy_vallejo_household" {
  target = authentik_application.grocy_vallejo.uuid
  group  = authentik_group.grocy_vallejo_household.id
  order  = 0
}

moved {
  from = authentik_policy_binding.grocy_vallejo_admins
  to   = authentik_policy_binding.grocy_vallejo_household
}

resource "authentik_provider_oauth2" "grocy_mcp_vallejo" {
  name               = "grocy-mcp-vallejo"
  client_id          = "grocy-mcp-vallejo"
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
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://grocy-mcp-vallejo.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "grocy_mcp_vallejo" {
  name              = "Grocy MCP Vallejo"
  slug              = "grocy-mcp-vallejo"
  protocol_provider = authentik_provider_oauth2.grocy_mcp_vallejo.id
  meta_description  = "Auth-aware MCP server for Grocy Vallejo household"
  meta_launch_url   = "https://grocy-mcp-vallejo.allegedly.works"
}

resource "authentik_policy_binding" "grocy_mcp_vallejo_household" {
  target = authentik_application.grocy_mcp_vallejo.uuid
  group  = authentik_group.grocy_vallejo_household.id
  order  = 0
}

moved {
  from = authentik_policy_binding.grocy_mcp_vallejo_admins
  to   = authentik_policy_binding.grocy_mcp_vallejo_household
}

resource "kubernetes_secret" "grocy_mcp_oidc_vallejo" {
  metadata {
    name      = "grocy-mcp-oidc-vallejo"
    namespace = "grocy-vallejo"
  }

  data = {
    client_id             = authentik_provider_oauth2.grocy_mcp_vallejo.client_id
    client_secret         = authentik_provider_oauth2.grocy_mcp_vallejo.client_secret
    grocy_proxy_client_id = authentik_provider_proxy.grocy_vallejo.client_id
  }
}

# --- Tana MCP facade (public OAuth facade, downstream static bearer) ---

resource "authentik_provider_oauth2" "tana_mcp_facade" {
  name               = "tana-mcp-facade"
  client_id          = "tana-mcp-facade"
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
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://tana-mcp-facade.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "tana_mcp_facade" {
  name              = "Tana MCP Facade"
  slug              = "tana-mcp-facade"
  protocol_provider = authentik_provider_oauth2.tana_mcp_facade.id
  meta_description  = "Public OAuth facade for the Tana MCP server"
  meta_launch_url   = "https://tana-mcp-facade.allegedly.works"
}

resource "authentik_group" "tana_agentydragon_gmail_com_account_access" {
  name  = "tana-agentydragon-gmail-com-account-access"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_policy_binding" "tana_mcp_facade_account_access" {
  target = authentik_application.tana_mcp_facade.uuid
  group  = authentik_group.tana_agentydragon_gmail_com_account_access.id
  order  = 0
}

resource "kubernetes_secret" "tana_mcp_facade_oidc" {
  metadata {
    name      = "tana-mcp-facade-oidc"
    namespace = "tana-mcp"
  }

  data = {
    client_id     = authentik_provider_oauth2.tana_mcp_facade.client_id
    client_secret = authentik_provider_oauth2.tana_mcp_facade.client_secret
  }
}

# --- Manifold MCP facade (public OAuth facade, gates access to Manifold Markets) ---
#
# Same shape as the Tana facade above: confidential OAuth2 client wrapped by
# OIDCProxy, restricted to agentydragon via a one-user group + policy binding.
# The downstream Manifold API is reached via a static MANIFOLD_API_KEY held by
# the manifold-mcp-server sidecar, not via Authentik.

resource "authentik_provider_oauth2" "manifold_mcp" {
  name               = "manifold-mcp"
  client_id          = "manifold-mcp"
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
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://manifold-mcp.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "manifold_mcp" {
  name              = "Manifold MCP Facade"
  slug              = "manifold-mcp"
  protocol_provider = authentik_provider_oauth2.manifold_mcp.id
  meta_description  = "OAuth facade for Manifold Markets MCP. Read+write surface (place_bet, send_mana, etc.); restricted to agentydragon."
  meta_launch_url   = "https://manifold-mcp.allegedly.works"
}

resource "authentik_group" "manifold_mcp_users" {
  name  = "manifold-mcp-users"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_policy_binding" "manifold_mcp_users" {
  target = authentik_application.manifold_mcp.uuid
  group  = authentik_group.manifold_mcp_users.id
  order  = 0
}

resource "kubernetes_secret" "manifold_mcp_oidc" {
  metadata {
    name      = "manifold-mcp-oidc"
    namespace = "manifold-mcp"
  }

  data = {
    client_id     = authentik_provider_oauth2.manifold_mcp.client_id
    client_secret = authentik_provider_oauth2.manifold_mcp.client_secret
  }
}

# --- PostScan Mail MCP facade (public OAuth facade, gates access to PostScan Mail) ---
#
# Same shape as the Manifold facade: confidential OAuth2 client wrapped by
# OIDCProxy, restricted to agentydragon via a one-user group + policy binding.
# The downstream PostScan Mail REST API is reached via a static x-api-key
# held by the postscanmail-mcp-server sidecar.

# Bump `keepers.version` to force the provider to be replaced, which
# regenerates the client_secret in Authentik and re-renders the
# `postscanmail-mcp-oidc` Kubernetes Secret. Reloader rolls the facade pod
# automatically on Secret change.
resource "random_id" "postscanmail_mcp_secret_rotation" {
  byte_length = 4
  keepers = {
    version = "2"
  }
}

resource "authentik_provider_oauth2" "postscanmail_mcp" {
  name               = "postscanmail-mcp"
  client_id          = "postscanmail-mcp"
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
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://postscanmail-mcp.allegedly.works/auth/callback"
    },
  ]

  lifecycle {
    replace_triggered_by = [random_id.postscanmail_mcp_secret_rotation]
  }
}

resource "authentik_application" "postscanmail_mcp" {
  name              = "PostScan Mail MCP Facade"
  slug              = "postscanmail-mcp"
  protocol_provider = authentik_provider_oauth2.postscanmail_mcp.id
  meta_description  = "OAuth facade for the PostScan Mail Developer API. Read+write surface (list_items, request_open/discard/rescan/shred — paid actions); restricted to agentydragon."
  meta_launch_url   = "https://postscanmail-mcp.allegedly.works"
}

resource "authentik_group" "postscanmail_mcp_users" {
  name  = "postscanmail-mcp-users"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_policy_binding" "postscanmail_mcp_users" {
  target = authentik_application.postscanmail_mcp.uuid
  group  = authentik_group.postscanmail_mcp_users.id
  order  = 0
}

resource "kubernetes_secret" "postscanmail_mcp_oidc" {
  metadata {
    name      = "postscanmail-mcp-oidc"
    namespace = "postscanmail-mcp"
  }

  data = {
    client_id     = authentik_provider_oauth2.postscanmail_mcp.client_id
    client_secret = authentik_provider_oauth2.postscanmail_mcp.client_secret
  }
}

# --- Plaid DB MCP facade (public OAuth facade over read-only Postgres MCP) ---

resource "authentik_provider_oauth2" "plaid_db_mcp" {
  name               = "plaid-db-mcp"
  client_id          = "plaid-db-mcp"
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
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://plaid-db.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "plaid_db_mcp" {
  name              = "Plaid DB MCP Facade"
  slug              = "plaid-db-mcp"
  protocol_provider = authentik_provider_oauth2.plaid_db_mcp.id
  meta_description  = "OAuth facade for read-only SQL access to the synced Plaid Postgres database; restricted to agentydragon."
  meta_launch_url   = "https://plaid-db.allegedly.works"
}

resource "authentik_group" "plaid_db_mcp_users" {
  name  = "plaid-db-mcp-users"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_policy_binding" "plaid_db_mcp_users" {
  target = authentik_application.plaid_db_mcp.uuid
  group  = authentik_group.plaid_db_mcp_users.id
  order  = 0
}

resource "kubernetes_secret" "plaid_db_mcp_oidc" {
  metadata {
    name      = "plaid-db-mcp-oidc"
    namespace = "plaid-mcp"
  }

  data = {
    client_id     = authentik_provider_oauth2.plaid_db_mcp.client_id
    client_secret = authentik_provider_oauth2.plaid_db_mcp.client_secret
  }
}

# ============================================================================
# Shared: kubectl-sandbox-users group
# ============================================================================

data "authentik_user" "agentydragon" {
  username = "agentydragon"
}

data "authentik_user" "auragon" {
  username = "auragon"
}

resource "authentik_group" "kubectl_sandbox_users" {
  name  = "kubectl-sandbox-users"
  users = [data.authentik_user.agentydragon.pk]
}

# ============================================================================
# kubectl-passthrough-mcp — Passthrough kubectl MCP (caller's own permissions)
# ============================================================================
# Forwards the caller's Authentik JWT directly to kube-apiserver. The caller
# gets their own OIDC permissions — not sandbox-scoped.

resource "authentik_provider_oauth2" "kubectl_passthrough_mcp" {
  name               = "kubectl-passthrough-mcp"
  client_id          = "kubectl-passthrough-mcp"
  client_type        = "public" # PKCE-based; no client secret distributed to users.
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  # - Claude Code: http://localhost:<port>/callback
  # - kubernetes-mcp-server's built-in callback (browser testing): /oauth/callback
  #
  # TODO: Consider adding https://claude.ai/api/mcp/auth_callback for Claude.ai
  # Custom Connectors. Intentionally omitted for now — using the passthrough
  # server from Claude.ai would give the caller full (admin) cluster access
  # through the web UI, which may be more exposure than we want. The
  # kubectl-sandbox-mcp provider includes it because its scope mapping keeps
  # the caller sandbox-only regardless.
  allowed_redirect_uris = [
    {
      matching_mode = "regex"
      url           = "^http://localhost:[0-9]+/callback$"
    },
    {
      matching_mode = "strict"
      url           = "https://kubectl-passthrough-mcp.allegedly.works/oauth/callback"
    },
  ]
}

moved {
  from = authentik_provider_oauth2.kubectl_sandbox_mcp
  to   = authentik_provider_oauth2.kubectl_passthrough_mcp
}

resource "authentik_application" "kubectl_passthrough_mcp" {
  name              = "kubectl-passthrough-mcp"
  slug              = "kubectl-passthrough-mcp"
  protocol_provider = authentik_provider_oauth2.kubectl_passthrough_mcp.id
  meta_description  = "Passthrough kubectl MCP — forwards caller's JWT to kube-apiserver (caller's own permissions)"
  meta_launch_url   = "https://kubectl-passthrough-mcp.allegedly.works"
}

moved {
  from = authentik_application.kubectl_sandbox_mcp
  to   = authentik_application.kubectl_passthrough_mcp
}

resource "authentik_policy_binding" "kubectl_passthrough_mcp_users" {
  target = authentik_application.kubectl_passthrough_mcp.uuid
  group  = authentik_group.kubectl_sandbox_users.id
  order  = 0
}

moved {
  from = authentik_policy_binding.kubectl_sandbox_mcp_users
  to   = authentik_policy_binding.kubectl_passthrough_mcp_users
}

## Passthrough server needs no Secret — the OAuth2 provider is public (PKCE)
## so there's no client_secret, and passthrough mode doesn't do token exchange.
## The previous kubernetes_secret.kubectl_passthrough_mcp was deleted above;
## TF will destroy the state-only resource on the next apply.

# ============================================================================
# kubectl-sandbox-mcp — Scoped kubectl MCP (token exchange → sandbox only)
# ============================================================================
# Authenticates callers via Authentik consent, then exchanges the caller's
# token for one scoped to kubectl-sandbox-users group. Even if the caller is
# a cluster admin, the exchanged token only carries sandbox-level permissions.

## User-facing public OAuth2 client (Claude Code / other MCP clients). No
## client secret — relies on PKCE. Users only need client_id to connect.
resource "authentik_provider_oauth2" "kubectl_sandbox_scoped" {
  name               = "kubectl-sandbox-mcp"
  client_id          = "kubectl-sandbox-mcp"
  client_type        = "public"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # Custom scope mapping overrides the `groups` claim to a fixed
  # `["kubectl-sandbox-users"]` regardless of the authenticating user's actual
  # groups. This achieves privilege scoping at token-issue time — no RFC 8693
  # exchange needed, no confidential secondary client. Even a cluster admin
  # who logs in here only gets sandbox-level RBAC (via the OIDC group claim
  # mapping in kube-apiserver's AuthenticationConfiguration).
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
    authentik_property_mapping_provider_scope.kubectl_sandbox_fixed_groups.id,
  ]

  # - Claude Code: http://localhost:<port>/callback
  # - Claude.ai web (custom connectors): https://claude.ai/api/mcp/auth_callback
  # - kubernetes-mcp-server's built-in callback (browser testing): /oauth/callback
  allowed_redirect_uris = [
    {
      matching_mode = "regex"
      url           = "^http://localhost:[0-9]+/callback$"
    },
    {
      matching_mode = "strict"
      url           = "https://claude.ai/api/mcp/auth_callback"
    },
    {
      matching_mode = "strict"
      url           = "https://kubectl-sandbox-mcp.allegedly.works/oauth/callback"
    },
  ]
}

## Scope mapping: hardcodes `groups = ["kubectl-sandbox-users"]` on the issued
## token. This is what makes kubectl-sandbox-mcp scope-safe: the user's actual
## group memberships are NOT forwarded into the token; kube-apiserver only
## sees the sandbox group.
resource "authentik_property_mapping_provider_scope" "kubectl_sandbox_fixed_groups" {
  name       = "kubectl-sandbox-mcp-fixed-groups"
  scope_name = "groups"
  expression = <<-EXPR
    # Overrides the user's real groups with a fixed sandbox-only group.
    # The kubectl-sandbox-mcp application authenticates users via consent,
    # but the issued token only carries this group — no privilege escalation.
    return {"groups": ["kubectl-sandbox-users"]}
  EXPR
}

## Machine-client scope mapping for kubectl-sandbox-client-credentials.
## This provider is trusted by kube-apiserver, so keep the effective groups as
## an explicit allowlist of known machine principals. Unknown users get no
## Kubernetes RBAC group instead of falling back to sandbox.
resource "authentik_property_mapping_provider_scope" "kubectl_machine_groups" {
  name       = "kubectl-client-credentials-machine-groups"
  scope_name = "groups"
  expression = <<-EXPR
    username = request.user.username
    if username == "ak-kubectl-sandbox-client-credentials-client_credentials":
        return {"groups": ["kubectl-sandbox-users"]}
    if username == "haku-k8s":
        return {"groups": ["haku"]}
    return {"groups": []}
  EXPR
}

resource "authentik_application" "kubectl_sandbox_scoped" {
  name              = "kubectl-sandbox-mcp"
  slug              = "kubectl-sandbox-mcp"
  protocol_provider = authentik_provider_oauth2.kubectl_sandbox_scoped.id
  meta_description  = "Sandbox kubectl MCP — token issued with fixed sandbox group"
  meta_launch_url   = "https://kubectl-sandbox-mcp.allegedly.works"
}

resource "authentik_policy_binding" "kubectl_sandbox_scoped_users" {
  target = authentik_application.kubectl_sandbox_scoped.uuid
  group  = authentik_group.kubectl_sandbox_users.id
  order  = 0
}

## No Kubernetes Secret needed — the OAuth2 provider is public (PKCE) and
## the pod runs in plain passthrough mode (no token exchange). The previous
## kubernetes_secret.kubectl_sandbox_scoped resource will be destroyed on
## the next TF apply.

# ============================================================================
# kubectl-sandbox-client-credentials — machine-to-machine OIDC for kubeconfig
# ============================================================================
# Non-interactive sibling of kubectl-sandbox-mcp: confidential OAuth2 client
# using `client_credentials` grant, so a CronJob can mint JWTs without a
# browser. Reuses the same kube-apiserver trusted issuer/audience for every
# machine identity; the provider's machine-only scope mapping is an explicit
# principal-to-groups allowlist.
#
# Consumer: cluster/k8s/agents/authentik-jwt-rotation/ CronJob. It runs
# biweekly, exchanges client_id + client_secret for a JWT, commits the JWT
# SOPS-encrypted to secrets/claude-web-k8s-jwt.yaml. `write_kubeconfig.py`
# on Claude Code sessions decrypts and embeds it.

resource "authentik_provider_oauth2" "kubectl_sandbox_client_credentials" {
  name        = "kubectl-sandbox-client-credentials"
  client_id   = "kubectl-sandbox-client-credentials"
  client_type = "confidential" # client_secret auto-generated by Authentik

  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # 45d access-token validity — comfortable margin over the biweekly rotation
  # CronJob. See cluster/k8s/agents/authentik-jwt-rotation/ for cadence math.
  access_token_validity = "hours=1080"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    authentik_property_mapping_provider_scope.kubectl_machine_groups.id,
  ]

  # client_credentials doesn't redirect, so allowed_redirect_uris is omitted.
}

resource "authentik_application" "kubectl_sandbox_client_credentials" {
  name              = "kubectl-sandbox-client-credentials"
  slug              = "kubectl-sandbox-client-credentials"
  protocol_provider = authentik_provider_oauth2.kubectl_sandbox_client_credentials.id
  meta_description  = "Machine-to-machine kubectl access with explicit Authentik principal-to-group mapping"
}

# Authentik auto-creates an internal service account for client_credentials
# grants (username: ak-<slug>-client_credentials). The client_credentials flow
# authenticates as this user. Without a policy binding for it, the grant is
# rejected with invalid_grant.
data "authentik_user" "kubectl_sandbox_cc_auto" {
  username = "ak-kubectl-sandbox-client-credentials-client_credentials"
}

resource "authentik_policy_binding" "kubectl_sandbox_cc_auto_user" {
  target = authentik_application.kubectl_sandbox_client_credentials.uuid
  user   = data.authentik_user.kubectl_sandbox_cc_auto.pk
  order  = 0
}

# K8s Secret holding the client_id + client_secret. Lives in agents-infra
# (where the authentik-jwt-rotation CronJob runs) and is the only place this
# credential exists outside Authentik — the CC web sandbox only ever holds
# the already-minted JWT (in secrets/claude-web-k8s-jwt.yaml SOPS).
resource "kubernetes_secret" "kubectl_sandbox_client_credentials" {
  metadata {
    name      = "kubectl-sandbox-client-credentials"
    namespace = "agents-infra"
    annotations = {
      description = "client_id + client_secret for kubectl-sandbox-client-credentials OIDC provider (mounted by authentik-jwt-rotation CronJob)"
    }
  }

  data = {
    client_id     = authentik_provider_oauth2.kubectl_sandbox_client_credentials.client_id
    client_secret = authentik_provider_oauth2.kubectl_sandbox_client_credentials.client_secret
  }
}

# ============================================================================
# haku-k8s — Haku's k8s identity, reusing the shared client_credentials provider
# ============================================================================
# Haku mints its k8s JWT through the EXISTING
# kubectl-sandbox-client-credentials OAuth2 provider — no dedicated provider,
# so no kube-apiserver issuer entry and no cluster bootstrap. Instead of a
# provider-level client_secret, the authentik-jwt-rotation CronJob authenticates
# as the haku-k8s service account using that SA's app-password token paired with
# the shared provider's client_id. Authentik issues a token FOR the haku-k8s SA,
# and the provider's machine-only scope mapping emits groups: ["haku"] only for
# that exact username. Unknown machine principals receive no Kubernetes group.
# The apiserver maps that claim to oidc-ksbx-groups:haku — read-only access to
# the haku namespace, isolated from the sandbox group.
#
# Consumer: cluster/k8s/agents/authentik-jwt-rotation/ CronJob (the haku-k8s
# rotations.yaml entry). It exchanges client_id + username + app-password for a
# JWT and commits it SOPS-encrypted to secrets/haku-k8s-jwt.yaml.

# Service account whose identity the issued JWT carries. The machine scope
# mapping branches on this username (haku-k8s) to emit groups: ["haku"].
resource "authentik_user" "haku_k8s" {
  username = "haku-k8s"
  name     = "Haku k8s service account"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

# App-password token for the haku-k8s SA, used as the password in the
# client_credentials username/password exchange against the shared provider's
# client_id.
resource "authentik_token" "haku_k8s" {
  identifier   = "haku-k8s-client-credentials"
  user         = authentik_user.haku_k8s.id
  intent       = "app_password"
  expiring     = false
  retrieve_key = true
  description  = "client_credentials app-password for Haku's k8s JWT rotation"
}

# Authorize the haku-k8s SA on the shared client_credentials application so the
# grant is accepted. Mirrors the existing per-user bindings on this app; uses a
# distinct order to avoid collisions.
resource "authentik_policy_binding" "haku_k8s_client_credentials" {
  target = authentik_application.kubectl_sandbox_client_credentials.uuid
  user   = authentik_user.haku_k8s.id
  order  = 2
}

# K8s Secret holding the shared provider's client_id + the haku-k8s username
# and app-password. Lives in agents-infra (where the authentik-jwt-rotation
# CronJob runs) and is the only place this credential exists outside Authentik
# — the minted JWT lives SOPS-encrypted in secrets/haku-k8s-jwt.yaml.
resource "kubernetes_secret" "haku_client_credentials" {
  metadata {
    name      = "haku-client-credentials"
    namespace = "agents-infra"
    annotations = {
      description = "client_id (shared kubectl-sandbox-client-credentials provider) + haku-k8s username/app-password, authenticating the haku-k8s service account so its JWT carries groups: [\"haku\"] (mounted by authentik-jwt-rotation CronJob)"
    }
  }

  data = {
    client_id = authentik_provider_oauth2.kubectl_sandbox_client_credentials.client_id
    username  = authentik_user.haku_k8s.username
    password  = authentik_token.haku_k8s.key
  }
}
