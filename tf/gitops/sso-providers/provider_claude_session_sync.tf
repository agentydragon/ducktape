# The Claude session sync's pairing page (claude-session-sync.allegedly.works) handles its own
# Authentik login: the app owns the OIDC code exchange and keeps only a signed, short-lived cookie.
# The page pairs a Claude OAuth grant, so it is scoped to the account owner only, twice: this
# application's policy binding and the allowed `sub` the app itself checks.
# cluster/cdk8s/claude_session_sync.py consumes the Secret below and spells the same origin.

resource "random_password" "claude_session_sync_session_secret" {
  length  = 64
  special = false
}

resource "authentik_application" "claude_session_sync" {
  name              = "Claude session sync"
  slug              = "claude-session-sync"
  protocol_provider = authentik_provider_oauth2.providers["claude_session_sync"].id
  meta_description  = "Pair the Claude Code session sync and watch it mirror sessions into Postgres"
  meta_launch_url   = "https://claude-session-sync.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "claude_session_sync_owner_only" {
  target = authentik_application.claude_session_sync.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}
