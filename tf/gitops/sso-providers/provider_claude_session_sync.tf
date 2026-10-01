# The Claude session sync's pairing page (claude-session-sync.allegedly.works) handles its own
# Authentik login: the app owns the OIDC code exchange and keeps only a signed, short-lived cookie.
# The page pairs a Claude OAuth grant, so it is scoped to the account owner only, twice: this
# application's policy binding and the allowed `sub` the app itself checks.
# cluster/cdk8s/claude_session_sync/app.py consumes the Secret below and spells the same origin.

resource "random_password" "claude_session_sync_session_secret" {
  length  = 64
  special = false
}

resource "authentik_provider_oauth2" "claude_session_sync" {
  name               = "claude-session-sync-oidc"
  client_id          = "claude-session-sync"
  client_type        = "confidential"
  grant_types        = ["authorization_code"]
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  # The app's allowlist below is this same immutable numeric user ID.
  sub_mode              = "user_id"
  access_token_validity = "hours=1"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.email.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode     = "strict"
      url               = "https://claude-session-sync.allegedly.works/auth/callback"
      redirect_uri_type = "authorization"
    }
  ]
}

resource "authentik_application" "claude_session_sync" {
  name              = "Claude session sync"
  slug              = "claude-session-sync"
  protocol_provider = authentik_provider_oauth2.claude_session_sync.id
  meta_description  = "Pair the Claude Code session sync and watch it mirror sessions into Postgres"
  meta_launch_url   = "https://claude-session-sync.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "claude_session_sync_owner_only" {
  target = authentik_application.claude_session_sync.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

# Reflector mirrors this into the app's namespace. The keys are the environment variables of the
# web app's `WebSettings` (devinfra/claude/session_export/settings.py), which the Deployment reads
# from them one by one.
resource "kubernetes_secret" "claude_session_sync_oidc" {
  metadata {
    name      = "claude-session-sync-oidc"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "claude-session-sync"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "claude-session-sync"
    }
  }

  data = {
    SESSION_SYNC_OIDC_ISSUER          = "https://auth.allegedly.works/application/o/claude-session-sync/"
    SESSION_SYNC_OIDC_CLIENT_ID       = authentik_provider_oauth2.claude_session_sync.client_id
    SESSION_SYNC_OIDC_CLIENT_SECRET   = authentik_provider_oauth2.claude_session_sync.client_secret
    SESSION_SYNC_OIDC_SESSION_SECRET  = random_password.claude_session_sync_session_secret.result
    SESSION_SYNC_OIDC_ALLOWED_SUBJECT = tostring(authentik_user.agentydragon.id)
  }
}
