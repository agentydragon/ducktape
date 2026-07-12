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
