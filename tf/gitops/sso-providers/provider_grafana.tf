# Grafana — OIDC login for the monitoring dashboard

resource "authentik_application" "grafana" {
  name              = "Grafana"
  slug              = "grafana"
  protocol_provider = authentik_provider_oauth2.confidential["grafana"].id
  meta_icon         = "https://cdn.simpleicons.org/grafana"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "grafana_admins" {
  target = authentik_application.grafana.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}
