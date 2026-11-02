resource "random_password" "openai_platform_session_secret" {
  length  = 64
  special = false
}

resource "authentik_provider_oauth2" "openai_platform" {
  name               = "openai-platform-oidc"
  client_id          = "openai-platform"
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
    url               = "https://openai-platform.allegedly.works/auth/callback"
    redirect_uri_type = "authorization"
  }]
}

resource "authentik_application" "openai_platform" {
  name              = "OpenAI Platform session"
  slug              = "openai-platform"
  protocol_provider = authentik_provider_oauth2.openai_platform.id
  meta_description  = "OpenAI Platform billing session status"
  meta_launch_url   = "https://openai-platform.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "openai_platform_owner" {
  target = authentik_application.openai_platform.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

resource "kubernetes_secret" "openai_platform_oidc" {
  metadata {
    name      = "openai-platform-oidc"
    namespace = "openai-platform-session"
  }
  data = {
    OPENAI_PLATFORM_ISSUER          = "https://auth.allegedly.works/application/o/openai-platform/"
    OPENAI_PLATFORM_CLIENT_ID       = authentik_provider_oauth2.openai_platform.client_id
    OPENAI_PLATFORM_CLIENT_SECRET   = authentik_provider_oauth2.openai_platform.client_secret
    OPENAI_PLATFORM_SESSION_SECRET  = random_password.openai_platform_session_secret.result
    OPENAI_PLATFORM_ALLOWED_SUBJECT = tostring(authentik_user.agentydragon.id)
  }
}
