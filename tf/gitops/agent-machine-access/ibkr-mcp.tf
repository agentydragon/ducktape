# IBKR market-data MCP: confidential OAuth2 client wrapped by the server's
# OIDCProxy, restricted to agentydragon via a one-user group + policy binding.
# claude.ai users and haku-console (operator OAuth) both authenticate through
# this provider; there is no machine token / direct_jwt_trust. The downstream
# IBKR gateway session is held by the co-located IBeam sidecar, not Authentik.

resource "authentik_provider_oauth2" "ibkr_mcp" {
  name               = "ibkr-mcp"
  client_id          = "ibkr-mcp"
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
      url           = "https://ibkr-mcp.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "ibkr_mcp" {
  name              = "IBKR Market Data MCP"
  slug              = "ibkr-mcp"
  protocol_provider = authentik_provider_oauth2.ibkr_mcp.id
  meta_description  = "OAuth front door for the read-only IBKR market-data MCP. Restricted to agentydragon."
  meta_launch_url   = "https://ibkr-mcp.allegedly.works"
}

resource "authentik_group" "ibkr_mcp_users" {
  name  = "ibkr-mcp-users"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_policy_binding" "ibkr_mcp_users" {
  target = authentik_application.ibkr_mcp.uuid
  group  = authentik_group.ibkr_mcp_users.id
  order  = 0
}

resource "kubernetes_secret" "ibkr_mcp_oidc" {
  metadata {
    name      = "ibkr-mcp-oidc"
    namespace = "ibkr"
  }

  data = {
    client_id     = authentik_provider_oauth2.ibkr_mcp.client_id
    client_secret = authentik_provider_oauth2.ibkr_mcp.client_secret
  }
}
