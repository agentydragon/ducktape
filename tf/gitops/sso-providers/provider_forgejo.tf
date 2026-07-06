# Forgejo — OIDC login for the git server

resource "authentik_provider_oauth2" "forgejo" {
  name               = "forgejo-oauth2"
  client_id          = "forgejo"
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
      url           = "https://git.allegedly.works/user/oauth2/authentik/callback"
    },
  ]
}

resource "authentik_application" "forgejo" {
  name              = "Forgejo"
  slug              = "forgejo"
  protocol_provider = authentik_provider_oauth2.forgejo.id
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

# Forgejo helm chart expects keys named "key" (client_id) and "secret" (client_secret).
resource "kubernetes_secret" "forgejo_oauth" {
  metadata {
    name      = "forgejo-oauth-client-secret"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "forgejo,flux-system"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "forgejo,flux-system"
    }
  }

  data = {
    key    = authentik_provider_oauth2.forgejo.client_id
    secret = authentik_provider_oauth2.forgejo.client_secret
  }
}
