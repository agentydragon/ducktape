# ============================================================================
# Grafana — OIDC login for the monitoring dashboard
# ============================================================================

resource "authentik_provider_oauth2" "grafana" {
  name               = "grafana-oauth2"
  client_id          = "grafana"
  client_type        = "confidential"
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
      url           = "https://grafana.allegedly.works/login/generic_oauth"
    },
  ]
}

resource "authentik_application" "grafana" {
  name              = "Grafana"
  slug              = "grafana"
  protocol_provider = authentik_provider_oauth2.grafana.id
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
    GF_AUTH_GENERIC_OAUTH_CLIENT_ID     = authentik_provider_oauth2.grafana.client_id
    GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET = authentik_provider_oauth2.grafana.client_secret
  }
}
