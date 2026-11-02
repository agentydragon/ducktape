# Quest's status page handles OIDC itself; the client and signed-session keys
# are scoped to its namespace. The Authentik binding and app both admit only
# the account owner.
resource "random_password" "quest_diagnostics_session_secret" {
  length  = 64
  special = false
}

resource "authentik_provider_oauth2" "quest_diagnostics" {
  name               = "quest-diagnostics-oidc"
  client_id          = "quest-diagnostics"
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
    url               = "https://quest.allegedly.works/auth/callback"
    redirect_uri_type = "authorization"
  }]
}

resource "authentik_application" "quest_diagnostics" {
  name              = "Quest appointments"
  slug              = "quest-diagnostics"
  protocol_provider = authentik_provider_oauth2.quest_diagnostics.id
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
    QUEST_OIDC_ISSUER          = "https://auth.allegedly.works/application/o/quest-diagnostics/"
    QUEST_OIDC_CLIENT_ID       = authentik_provider_oauth2.quest_diagnostics.client_id
    QUEST_OIDC_CLIENT_SECRET   = authentik_provider_oauth2.quest_diagnostics.client_secret
    QUEST_OIDC_SESSION_SECRET  = random_password.quest_diagnostics_session_secret.result
    QUEST_OIDC_ALLOWED_SUBJECT = tostring(authentik_user.agentydragon.id)
  }
}
