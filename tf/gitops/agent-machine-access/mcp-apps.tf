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

  # grocy_mcp_haku_sf federates here too, so haku's machine token can be exchanged
  # for a grocy-sf proxy token (the MCP performs that jwt-bearer exchange).
  jwt_federation_providers = [
    authentik_provider_oauth2.grocy_mcp_sf.id,
    authentik_provider_oauth2.grocy_mcp_haku_sf.id,
  ]
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

# Dedicated machine client_credentials provider for haku's read-only grocy-sf MCP
# token, kept separate from the user-facing grocy-mcp-sf so its long access-token
# validity doesn't lengthen claude.ai user tokens. It SHARES grocy-mcp-sf's
# self_signed signing key, so the grocy-mcp-sf JWKS (which the MCP's JWTVerifier is
# configured from) already validates tokens minted here — the MCP only has to also
# accept this provider's issuer (GROCY_MCP_AUTH__EXTRA_JWT_ISSUERS on the grocy-sf
# MCP deployment). The grocy-sf proxy federates this provider too (above), so the
# MCP's jwt-bearer exchange into the proxy works; the haku SA carries username
# `haku`, which the outpost forwards to Grocy.
resource "authentik_provider_oauth2" "grocy_mcp_haku_sf" {
  name        = "grocy-mcp-haku-sf"
  client_id   = "grocy-mcp-haku-sf"
  client_type = "confidential"

  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # 30d access-token validity — comfortable margin over the rotation CronJob's 24h
  # re-mint threshold (re-mints ~every 29 days). See cluster/k8s/agents/authentik-jwt-rotation/.
  access_token_validity = "days=30"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]

  # client_credentials doesn't redirect, so allowed_redirect_uris is omitted.
}

resource "authentik_application" "grocy_mcp_haku_sf" {
  name              = "Grocy MCP Haku (SF)"
  slug              = "grocy-mcp-haku-sf"
  protocol_provider = authentik_provider_oauth2.grocy_mcp_haku_sf.id
  meta_description  = "Machine client_credentials provider for haku's read-only grocy-sf MCP access"
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
