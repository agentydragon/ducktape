resource "random_password" "platform_sessions_session_secret" {
  length  = 64
  special = false
}

resource "authentik_provider_oauth2" "platform_sessions" {
  name               = "platform-sessions-oidc"
  client_id          = "platform-sessions"
  client_type        = "confidential"
  grant_types        = ["authorization_code"]
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  sub_mode                   = "user_id"
  access_token_validity      = "hours=1"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.email.id,
  ]

  allowed_redirect_uris = [{
    matching_mode     = "strict"
    url               = "https://platform-sessions.allegedly.works/auth/callback"
    redirect_uri_type = "authorization"
  }]
}

resource "authentik_application" "platform_sessions" {
  name              = "Platform sessions"
  slug              = "platform-sessions"
  protocol_provider = authentik_provider_oauth2.platform_sessions.id
  meta_description  = "OpenAI, Anthropic, and Quest session status"
  meta_launch_url   = "https://platform-sessions.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "platform_sessions_owner" {
  target = authentik_application.platform_sessions.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

resource "kubernetes_secret" "platform_sessions_oidc" {
  metadata {
    name      = "platform-sessions-oidc"
    namespace = "platform-sessions"
  }
  data = {
    PLATFORM_SESSIONS_ISSUER          = "https://auth.allegedly.works/application/o/platform-sessions/"
    PLATFORM_SESSIONS_CLIENT_ID       = authentik_provider_oauth2.platform_sessions.client_id
    PLATFORM_SESSIONS_CLIENT_SECRET   = authentik_provider_oauth2.platform_sessions.client_secret
    PLATFORM_SESSIONS_SESSION_SECRET  = random_password.platform_sessions_session_secret.result
    PLATFORM_SESSIONS_ALLOWED_SUBJECT = tostring(authentik_user.agentydragon.id)
  }
}

moved {
  from = random_password.openai_platform_session_secret
  to   = random_password.platform_sessions_session_secret
}

moved {
  from = authentik_provider_oauth2.openai_platform
  to   = authentik_provider_oauth2.platform_sessions
}

moved {
  from = authentik_application.openai_platform
  to   = authentik_application.platform_sessions
}

moved {
  from = authentik_policy_binding.openai_platform_owner
  to   = authentik_policy_binding.platform_sessions_owner
}

# The old namespace is retired by gaffer-private. Avoid deleting its Secret after
# the workspace has moved its Kubernetes permissions to the new namespace.
removed {
  from = kubernetes_secret.openai_platform_oidc
  lifecycle {
    destroy = false
  }
}
