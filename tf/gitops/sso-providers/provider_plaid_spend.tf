# Plaid Spend uses a public native OAuth client for GNOME. The server computes
# one shared view from the SOPS-managed cluster card configuration.

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
  meta_launch_url   = "https://plaid-spend.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "plaid_spend_desktop_access" {
  target = authentik_application.plaid_spend_desktop.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}
