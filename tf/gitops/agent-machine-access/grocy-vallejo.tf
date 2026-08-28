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

# Canonical OIDC client credentials. Reflector mirrors this Secret into the
# household namespace after that namespace has been created.
resource "kubernetes_secret" "grocy_mcp_oidc_vallejo_source" {
  metadata {
    name      = "grocy-mcp-oidc-vallejo"
    namespace = "authentik"
    annotations = {
      description                                                     = "Grocy Vallejo MCP OIDC client credentials"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "grocy-vallejo"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "grocy-vallejo"
    }
  }

  data = {
    client_id             = authentik_provider_oauth2.grocy_mcp_vallejo.client_id
    client_secret         = authentik_provider_oauth2.grocy_mcp_vallejo.client_secret
    grocy_proxy_client_id = authentik_provider_proxy.grocy_vallejo.client_id
  }
}


# --- Tana MCP facade (public OAuth facade, downstream static bearer) ---
