# ============================================================================
# haku-console — app-owned OAuth (retires the forward-auth proxy outpost)
# ============================================================================
# haku-console (https://haku.allegedly.works) now authenticates its own surface
# instead of sitting behind the shared Authentik proxy outpost (the
# `haku-dashboard` proxy provider is tombstoned in
# cluster/k8s/authentik/app/blueprints/haku-dashboard-sso.yaml). Two OAuth2
# providers, both gated to agentydragon by the access group below:
#   - haku-console-mcp: the upstream confidential client for the console's
#     embedded FastMCP OIDCProxy, which presents DCR to claude.ai / the `claude`
#     CLI on /mcp. offline_access so Authentik issues refresh tokens (claude.ai
#     renews silently).
#   - haku-console: the operator browser-login relying party (authorization-code
#     -> the app's signed session cookie), replacing the outpost's forward-auth.
#     No offline_access (session RP).

resource "authentik_group" "haku_console_access" {
  name  = "haku-console-access"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_provider_oauth2" "haku_console_mcp" {
  name               = "haku-console-mcp"
  client_id          = "haku-console-mcp"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  # `sub` = the stable Authentik user id, identical across this and the operator-login provider, so
  # haku-console can key operator identity (associations + agent→operator link) on the opaque subject
  # rather than the mutable username, and the two providers' `sub` agree for the same user.
  sub_mode = "user_id"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  # FastMCP's OIDCProxy callback under the /mcp mount (matches airlock's /mcp/auth/callback).
  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://haku.allegedly.works/mcp/auth/callback"
    },
  ]
}

resource "authentik_application" "haku_console_mcp" {
  name              = "Haku Console MCP"
  slug              = "haku-console-mcp"
  protocol_provider = authentik_provider_oauth2.haku_console_mcp.id
  meta_description  = "OIDCProxy upstream auth for MCP clients (claude.ai / claude CLI) connecting to haku-console /mcp"
}

resource "authentik_policy_binding" "haku_console_mcp_access" {
  target = authentik_application.haku_console_mcp.uuid
  group  = authentik_group.haku_console_access.id
  order  = 0
}

resource "authentik_provider_oauth2" "haku_console_operator" {
  name               = "haku-console"
  client_id          = "haku-console"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  # Match haku_console_mcp: `sub` = the stable Authentik user id, so operator identity keys on the
  # opaque subject (consistent across both console providers), not the mutable username.
  sub_mode = "user_id"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://haku.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "haku_console_operator" {
  name              = "Haku"
  slug              = "haku-console"
  protocol_provider = authentik_provider_oauth2.haku_console_operator.id
  meta_description  = "Haku's interactive console (operator browser login)"
  meta_launch_url   = "https://haku.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "haku_console_operator_access" {
  target = authentik_application.haku_console_operator.uuid
  group  = authentik_group.haku_console_access.id
  order  = 0
}

# Signs the operator session cookie (Starlette SessionMiddleware). Generated here so it is
# stable across replicas/restarts (an ephemeral per-pod key would invalidate every other
# replica's sessions). 64 alphanumerics; no special chars (env-var safe).
resource "random_password" "haku_console_operator_session" {
  length  = 64
  special = false
}

resource "kubernetes_secret" "haku_console_oidc" {
  metadata {
    name      = "haku-console-oidc"
    namespace = "haku-console"
    annotations = {
      description = "haku-console OAuth client credentials: the MCP OIDCProxy upstream client (haku-console-mcp), the operator browser-login client (haku-console), and the operator session-cookie signing secret"
    }
  }

  data = {
    mcp_client_id           = authentik_provider_oauth2.haku_console_mcp.client_id
    mcp_client_secret       = authentik_provider_oauth2.haku_console_mcp.client_secret
    operator_client_id      = authentik_provider_oauth2.haku_console_operator.client_id
    operator_client_secret  = authentik_provider_oauth2.haku_console_operator.client_secret
    operator_session_secret = random_password.haku_console_operator_session.result
    # The operator's opaque OIDC subject (both providers run sub_mode=user_id → the Authentik user
    # id). haku-console's `haku` static agent executes operator_oauth calls as this operator
    # (config.yaml static_agents → HAKU_CONSOLE_AGENT_HAKU_OPERATOR); TF feeds the id so no opaque
    # value is hand-copied.
    operator_subject = tostring(data.authentik_user.agentydragon.pk)
  }
}
