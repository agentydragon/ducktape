# ============================================================================
# Harbor — OIDC login for the container registry
# ============================================================================

resource "authentik_provider_oauth2" "harbor" {
  name               = "harbor-oauth2"
  client_id          = "harbor"
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
      url           = "https://registry.allegedly.works/c/oidc/callback"
    },
  ]
}

resource "authentik_application" "harbor" {
  name              = "Harbor"
  slug              = "harbor"
  protocol_provider = authentik_provider_oauth2.harbor.id
  meta_description  = "Harbor Container Registry"
  meta_publisher    = "Harbor"
  meta_icon         = "https://cdn.simpleicons.org/harbor"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "harbor_admins" {
  target = authentik_application.harbor.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# Secret reflected to harbor namespace for harbor-oidc-config TF to read.
resource "kubernetes_secret" "harbor_oidc" {
  metadata {
    name      = "harbor-oauth-client-secret"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "harbor"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "harbor"
    }
  }

  data = {
    client_id     = authentik_provider_oauth2.harbor.client_id
    client_secret = authentik_provider_oauth2.harbor.client_secret
  }
}
