# Matrix — OIDC login for Element Web / Synapse

resource "authentik_application" "matrix" {
  name              = "Element"
  slug              = "matrix"
  protocol_provider = authentik_provider_oauth2.confidential["matrix"].id
  meta_description  = "Matrix Chat (Element Web)"
  meta_launch_url   = "https://chat.allegedly.works"
  meta_icon         = "https://cdn.simpleicons.org/element"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "matrix_admins" {
  target = authentik_application.matrix.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}
