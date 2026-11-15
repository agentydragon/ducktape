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

resource "kubernetes_secret" "grafana_oidc" {
  metadata {
    name      = "grafana-oidc-config"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "monitoring"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "monitoring"
    }
  }

  data = {
    GF_AUTH_GENERIC_OAUTH_CLIENT_ID     = authentik_provider_oauth2.confidential["grafana"].client_id
    GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET = authentik_provider_oauth2.confidential["grafana"].client_secret
  }
}
