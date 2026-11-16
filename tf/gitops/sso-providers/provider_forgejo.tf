# Forgejo — OIDC login for the git server

resource "authentik_application" "forgejo" {
  name              = "Forgejo"
  slug              = "forgejo"
  protocol_provider = authentik_provider_oauth2.confidential["forgejo"].id
  meta_description  = "Forgejo Git Repository Management"
  meta_publisher    = "Forgejo"
  meta_icon         = "https://cdn.simpleicons.org/forgejo"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "forgejo_admins" {
  target = authentik_application.forgejo.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}
