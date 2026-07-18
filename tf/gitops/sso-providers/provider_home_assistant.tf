# Home Assistant — public OIDC client with authorization-code flow and PKCE.
# The client has no secret to distribute. Home Assistant keeps its native auth
# provider enabled as a break-glass path if Authentik is unavailable.

resource "authentik_provider_oauth2" "home_assistant" {
  name               = "home-assistant-oauth2"
  client_id          = "home-assistant"
  client_type        = "public"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://home.allegedly.works/auth/oidc/callback"
    },
  ]
}

resource "authentik_application" "home_assistant" {
  name              = "Home Assistant"
  slug              = "home-assistant"
  protocol_provider = authentik_provider_oauth2.home_assistant.id
  meta_icon         = "https://cdn.simpleicons.org/homeassistant"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "home_assistant_users" {
  target = authentik_application.home_assistant.uuid
  group  = authentik_group.home_assistant_users.id
  order  = 0
}
