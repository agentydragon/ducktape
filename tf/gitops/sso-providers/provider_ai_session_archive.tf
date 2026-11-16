# The session archive browser uses the same owner-only Authentik login boundary as the
# application API, and also checks the immutable numeric subject in its own OIDC middleware.
resource "random_password" "ai_session_archive_session_secret" {
  length  = 64
  special = false
}

resource "authentik_provider_oauth2" "ai_session_archive" {
  name               = "ai-session-archive-oidc"
  client_id          = "ai-session-archive"
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

  allowed_redirect_uris = [
    {
      matching_mode     = "strict"
      url               = "https://ai-session-archive.allegedly.works/auth/callback"
      redirect_uri_type = "authorization"
    }
  ]
}

resource "authentik_application" "ai_session_archive" {
  name              = "AI session archive"
  slug              = "ai-session-archive"
  protocol_provider = authentik_provider_oauth2.ai_session_archive.id
  meta_description  = "Browse archived workstation AI sessions"
  meta_launch_url   = "https://ai-session-archive.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "ai_session_archive_owner_only" {
  target = authentik_application.ai_session_archive.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}
