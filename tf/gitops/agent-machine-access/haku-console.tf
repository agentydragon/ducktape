# ============================================================================
# haku-console — app-owned OAuth (retires the forward-auth proxy outpost)
# ============================================================================
# haku-console (https://haku.allegedly.works) now authenticates its own surface
# instead of sitting behind the retired `haku-dashboard` proxy outpost. Two OAuth2
# providers, both gated to agentydragon by the access group below:
#   - haku-console-mcp: the upstream confidential client for the console's
#     embedded FastMCP OIDCProxy, which presents DCR to claude.ai / the `claude`
#     CLI on /mcp. offline_access so Authentik issues refresh tokens (claude.ai
#     renews silently).
#   - haku-console: the operator browser-login relying party (authorization-code
#     -> the app's signed session cookie), replacing the outpost's forward-auth.
#     offline_access so Authentik issues a refresh token the console persists and
#     self-refreshes for hostexec (the operator's own token is exchanged for a
#     per-host hostexec token; see hostexec.tf).

resource "authentik_group" "haku_console_access" {
  name  = "haku-console-access"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_provider_oauth2" "haku_console_mcp" {
  name                  = "haku-console-mcp"
  client_id             = "haku-console-mcp"
  client_type           = "confidential"
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  signing_key           = data.authentik_certificate_key_pair.self_signed.id
  access_token_validity = "hours=1"

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  # `sub` = the stable Authentik user id, identical across this and the operator-login provider.
  # Haku verifies the exact issuer, resolves this external identity to a canonical Operator UUID,
  # and keys live associations/agent links on that UUID rather than the mutable username or bare sub.
  sub_mode = "user_id"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  # FastMCP's OIDCProxy callback under the /mcp mount.
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
  name                  = "haku-console"
  client_id             = "haku-console"
  client_type           = "confidential"
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  signing_key           = data.authentik_certificate_key_pair.self_signed.id
  access_token_validity = "hours=1"

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  # Match haku_console_mcp: `sub` = the stable Authentik user id. Exact issuer + subject resolves to
  # the same canonical Operator UUID; username remains display-only.
  sub_mode = "user_id"

  # offline_access so the operator login yields a refresh token the console persists + self-refreshes
  # for hostexec; the resulting access token is the client assertion the per-host providers federate.
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
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

# Canonical OAuth client credentials and session key. Reflector mirrors this
# Secret into haku-console after that namespace has been created.
resource "kubernetes_secret" "haku_console_oidc_source" {
  metadata {
    name      = "haku-console-oidc"
    namespace = "authentik"
    annotations = {
      description                                                     = "haku-console OAuth client credentials and operator session secret"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "haku-console"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "haku-console"
    }
  }

  data = {
    mcp_client_id           = authentik_provider_oauth2.haku_console_mcp.client_id
    mcp_client_secret       = authentik_provider_oauth2.haku_console_mcp.client_secret
    operator_client_id      = authentik_provider_oauth2.haku_console_operator.client_id
    operator_client_secret  = authentik_provider_oauth2.haku_console_operator.client_secret
    operator_session_secret = random_password.haku_console_operator_session.result
    # Controller-fed stable external user key for the shared Authentik user-id trust domain (both
    # providers run sub_mode=user_id). Haku resolves this to a canonical Operator UUID; the key is
    # used only during startup/migration and never carried as live request authority.
    # Externally stable label: this is only a startup seed for canonical Operator resolution,
    # never a live authorization key.
    operator_subject = tostring(data.authentik_user.agentydragon.pk)
  }
}


# Dedicated static-Agent bearer for public-coder-agent -> Haku Console MCP. The real value is
# delivered only to Haku Console and public-coder-agent's iron-proxy; the OpenClaw container gets
# a non-secret placeholder that the proxy replaces only for haku.allegedly.works Authorization
# headers. This Agent is assigned the explicit no-auto-approval policy in the console config, so
# possession of the bearer can submit calls but can never approve or execute one without the
# Operator's reviewed approval.
resource "random_password" "haku_console_public_coder_agent" {
  length  = 48
  special = false
}

# Canonical static-Agent bearer. Reflector mirrors it into both the Console and
# proxy namespaces so both hops use one generated value.
resource "kubernetes_secret" "haku_console_public_coder_agent_source" {
  metadata {
    name      = "haku-console-public-coder-agent"
    namespace = "authentik"
    annotations = {
      description                                                     = "Proxy-mediated static-Agent bearer for public-coder-agent -> Haku Console MCP"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "haku-console,public-coder-agent"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "haku-console,public-coder-agent"
    }
  }

  data = {
    token = random_password.haku_console_public_coder_agent.result
  }
}
