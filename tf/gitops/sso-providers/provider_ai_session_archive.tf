# The session archive browser uses the same owner-only Authentik login boundary as the
# application API, and also checks the immutable numeric subject in its own OIDC middleware.
resource "random_password" "ai_session_archive_session_secret" {
  length  = 64
  special = false
}

resource "authentik_application" "ai_session_archive" {
  name              = "AI session archive"
  slug              = "ai-session-archive"
  protocol_provider = authentik_provider_oauth2.providers["ai_session_archive"].id
  meta_description  = "Browse archived workstation AI sessions"
  meta_launch_url   = "https://ai-session-archive.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "ai_session_archive_owner_only" {
  target = authentik_application.ai_session_archive.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}
