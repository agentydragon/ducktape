# Quest's status page handles OIDC itself; the client and signed-session keys
# are scoped to its namespace. The Authentik binding and app both admit only
# the account owner.
resource "random_password" "quest_diagnostics_session_secret" {
  length  = 64
  special = false
}

resource "authentik_application" "quest_diagnostics" {
  name              = "Quest appointments"
  slug              = "quest-diagnostics"
  protocol_provider = authentik_provider_oauth2.providers["quest_diagnostics"].id
  meta_description  = "Quest appointment session status"
  meta_launch_url   = "https://quest.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "quest_diagnostics_owner" {
  target = authentik_application.quest_diagnostics.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

resource "kubernetes_secret" "quest_diagnostics_oidc" {
  metadata {
    name      = "quest-diagnostics-oidc"
    namespace = "quest-diagnostics"
  }
  data = {
    QUEST_OIDC_ISSUER          = "https://auth.allegedly.works/application/o/${authentik_application.quest_diagnostics.slug}/"
    QUEST_OIDC_CLIENT_ID       = authentik_provider_oauth2.providers["quest_diagnostics"].client_id
    QUEST_OIDC_CLIENT_SECRET   = authentik_provider_oauth2.providers["quest_diagnostics"].client_secret
    QUEST_OIDC_SESSION_SECRET  = random_password.quest_diagnostics_session_secret.result
    QUEST_OIDC_ALLOWED_SUBJECT = tostring(authentik_user.agentydragon.id)
  }
}
