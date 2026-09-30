# Plaid Spend has two OAuth clients: a public native client for GNOME, and a
# confidential web client for the shared settings page. Both providers use the
# same stable Authentik user UUID as `sub`, so settings are shared across them.

resource "authentik_provider_oauth2" "plaid_spend_desktop" {
  name               = "plaid-spend-desktop-oauth2"
  client_id          = "plaid-spend-desktop"
  client_type        = "public"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  sub_mode                   = "user_uuid"
  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  access_token_validity      = "minutes=15"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [{
    matching_mode     = "strict"
    url               = "http://127.0.0.1:43821/callback"
    redirect_uri_type = "authorization"
  }]
}

resource "authentik_application" "plaid_spend_desktop" {
  name              = "Plaid Spend Desktop"
  slug              = "plaid-spend-desktop"
  protocol_provider = authentik_provider_oauth2.plaid_spend_desktop.id
  meta_description  = "Native GNOME indicator for the shared Plaid card spend view"
  meta_launch_url   = "https://plaid-spend.allegedly.works/settings"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "plaid_spend_desktop_access" {
  target = authentik_application.plaid_spend_desktop.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

resource "random_password" "plaid_spend_session_secret" {
  length  = 64
  special = false
}

resource "authentik_provider_oauth2" "plaid_spend_web" {
  name               = "plaid-spend-web-oauth2"
  client_id          = "plaid-spend-web"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  sub_mode                   = "user_uuid"
  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  access_token_validity      = "hours=1"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.email.id,
  ]

  allowed_redirect_uris = [{
    matching_mode     = "strict"
    url               = "https://plaid-spend.allegedly.works/auth/callback"
    redirect_uri_type = "authorization"
  }]
}

resource "authentik_application" "plaid_spend_web" {
  name              = "Plaid Spend Settings"
  slug              = "plaid-spend"
  protocol_provider = authentik_provider_oauth2.plaid_spend_web.id
  meta_description  = "Configure the shared Plaid spend indicator"
  meta_launch_url   = "https://plaid-spend.allegedly.works/settings"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "plaid_spend_web_access" {
  target = authentik_application.plaid_spend_web.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

resource "kubernetes_secret" "plaid_spend_web_oidc" {
  metadata {
    name      = "plaid-spend-web-oidc-config"
    namespace = "authentik"
    annotations = {
      description = "Confidential OAuth client and signed-session secret for Plaid Spend settings"
    }
  }

  data = {
    client_id      = authentik_provider_oauth2.plaid_spend_web.client_id
    client_secret  = authentik_provider_oauth2.plaid_spend_web.client_secret
    session_secret = random_password.plaid_spend_session_secret.result
  }
}
