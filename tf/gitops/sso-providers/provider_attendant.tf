resource "random_password" "attendant_session_secret" {
  length  = 64
  special = false
}

resource "authentik_provider_oauth2" "attendant" {
  name               = "attendant-oidc"
  client_id          = "attendant"
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
    url               = "https://attendant.allegedly.works/auth/callback"
    redirect_uri_type = "authorization"
  }]
}

resource "authentik_application" "attendant" {
  name              = "Attendant"
  slug              = "attendant"
  protocol_provider = authentik_provider_oauth2.attendant.id
  meta_description  = "Owner-operated Chrome desktop"
  meta_launch_url   = "https://attendant.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "attendant_owner" {
  target = authentik_application.attendant.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

resource "kubernetes_secret" "attendant_oidc" {
  metadata {
    name      = "attendant-oidc"
    namespace = "attendant"
  }
  data = {
    ATTENDANT_ISSUER          = "https://auth.allegedly.works/application/o/${authentik_application.attendant.slug}/"
    ATTENDANT_CLIENT_ID       = authentik_provider_oauth2.attendant.client_id
    ATTENDANT_CLIENT_SECRET   = authentik_provider_oauth2.attendant.client_secret
    ATTENDANT_SESSION_SECRET  = random_password.attendant_session_secret.result
    ATTENDANT_ALLOWED_SUBJECT = tostring(authentik_user.agentydragon.id)
  }
}
