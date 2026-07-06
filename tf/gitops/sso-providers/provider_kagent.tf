# kagent — OIDC login for the kagent web UI (oauth2-proxy subchart)

resource "authentik_provider_oauth2" "kagent" {
  name               = "kagent"
  client_id          = "kagent"
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
      url           = "https://kagent.allegedly.works/oauth2/callback"
    },
  ]
}

resource "authentik_application" "kagent" {
  name              = "kagent"
  slug              = "kagent"
  protocol_provider = authentik_provider_oauth2.kagent.id
  meta_description  = "Kubernetes-native AI agent platform"
  meta_launch_url   = "https://kagent.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "kagent_admins" {
  target = authentik_application.kagent.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# oauth2-proxy needs a 32-byte cookie secret. Authentik doesn't supply one, so
# we generate it here and let the kubernetes_secret below carry it alongside
# the Authentik client_id/client_secret.
resource "random_password" "kagent_oauth2_proxy_cookie_secret" {
  length  = 32
  special = false
}

resource "kubernetes_secret" "kagent_oauth2_proxy" {
  metadata {
    name      = "kagent-oauth2-proxy"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "kagent"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "kagent"
    }
  }

  # Keys match what the upstream oauth2-proxy Helm chart expects when
  # config.existingSecret is set: client-id, client-secret, cookie-secret.
  data = {
    client-id     = authentik_provider_oauth2.kagent.client_id
    client-secret = authentik_provider_oauth2.kagent.client_secret
    cookie-secret = base64encode(random_password.kagent_oauth2_proxy_cookie_secret.result)
  }
}
