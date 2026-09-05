# aiquota owns an OAuth authorization-code flow; it is not an Authentik proxy
# application. The app verifies the Authentik-issued identity token and gates
# its own frontend entry point and browser API with a signed session cookie.
resource "authentik_group" "aiquota_access" {
  name  = "aiquota-access"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_provider_oauth2" "aiquota" {
  name                       = "aiquota"
  client_id                  = "aiquota"
  client_type                = "confidential"
  authorization_flow         = data.authentik_flow.implicit_consent.id
  invalidation_flow          = data.authentik_flow.invalidation.id
  signing_key                = data.authentik_certificate_key_pair.self_signed.id
  access_token_validity      = "hours=1"
  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  sub_mode                   = "user_id"
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]
  allowed_redirect_uris = [{ matching_mode = "strict", url = "https://aiquota.allegedly.works/auth/callback" }]
}

resource "authentik_application" "aiquota" {
  name              = "AI quota"
  slug              = "aiquota"
  protocol_provider = authentik_provider_oauth2.aiquota.id
  meta_description  = "AI subscription quota dashboard"
  meta_launch_url   = "https://aiquota.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "aiquota_access" {
  target = authentik_application.aiquota.uuid
  group  = authentik_group.aiquota_access.id
  order  = 0
}

resource "random_password" "aiquota_session" {
  length  = 64
  special = false
}

resource "kubernetes_secret" "aiquota_oidc_source" {
  metadata {
    name      = "aiquota-oidc"
    namespace = "authentik"
    annotations = {
      description                                                     = "aiquota OAuth client credentials and browser session signing secret"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "cli-proxy-api"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "cli-proxy-api"
    }
  }
  data = {
    client_id      = authentik_provider_oauth2.aiquota.client_id
    client_secret  = authentik_provider_oauth2.aiquota.client_secret
    session_secret = random_password.aiquota_session.result
  }
}
