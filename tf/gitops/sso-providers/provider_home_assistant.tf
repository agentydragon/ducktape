# Home Assistant — public OIDC client with authorization-code flow and PKCE.
# The client has no secret to distribute. Home Assistant keeps its native auth
# provider enabled as a break-glass path if Authentik is unavailable.

resource "authentik_application" "home_assistant" {
  name              = "Home Assistant"
  slug              = "home-assistant"
  protocol_provider = authentik_provider_oauth2.providers["home_assistant"].id
  meta_icon         = "https://cdn.simpleicons.org/homeassistant"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "home_assistant_users" {
  target = authentik_application.home_assistant.uuid
  group  = authentik_group.home_assistant_users.id
  order  = 0
}
