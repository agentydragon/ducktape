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
