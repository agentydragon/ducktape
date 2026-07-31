# HA-MCP has broad read/write access to Home Assistant through a server-side
# long-lived token. Keep its public MCP surface behind the shared OAuth facade
# and restrict consent to agentydragon.

resource "random_id" "ha_mcp_secret_rotation" {
  byte_length = 4
  keepers = {
    version = "1"
  }
}

resource "authentik_provider_oauth2" "ha_mcp" {
  name               = "ha-mcp"
  client_id          = "ha-mcp"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # Deviation: set explicitly because the Terraform provider defaults this to `minutes=10`, far
  # shorter than Authentik's own model default of `hours=1`. haku-console renews a linked account
  # 60s before expiry, so a 10-minute token meant ~150 token-endpoint calls per day per
  # association — and an ambiguous timeout on any single one of them permanently wedges that
  # association until an operator reconnects by hand. That is what silently disconnected this
  # server on 2026-07-28 and kubectl-passthrough-mcp on 2026-07-30. At 24h (matching the
  # `grocy-sf` / `grocy-vallejo` / `haku-ui` providers) it is ~1 renewal per day, cutting the
  # exposure ~144x. Refresh-token validity is unchanged at 30 days, so revocation is unaffected.
  access_token_validity = "hours=24"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://ha-mcp.allegedly.works/auth/callback"
    },
  ]

  lifecycle {
    replace_triggered_by = [random_id.ha_mcp_secret_rotation]
  }
}

resource "authentik_application" "ha_mcp" {
  name              = "Home Assistant MCP Facade"
  slug              = "ha-mcp"
  protocol_provider = authentik_provider_oauth2.ha_mcp.id
  meta_description  = "OAuth facade for the writable Home Assistant MCP server; restricted to agentydragon."
  meta_launch_url   = "https://ha-mcp.allegedly.works"
}

resource "authentik_group" "ha_mcp_users" {
  name  = "ha-mcp-users"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_policy_binding" "ha_mcp_users" {
  target = authentik_application.ha_mcp.uuid
  group  = authentik_group.ha_mcp_users.id
  order  = 0
}

resource "kubernetes_secret" "ha_mcp_oidc" {
  metadata {
    name      = "ha-mcp-oidc"
    namespace = "ha-mcp"
  }

  data = {
    client_id     = authentik_provider_oauth2.ha_mcp.client_id
    client_secret = authentik_provider_oauth2.ha_mcp.client_secret
  }
}
