# ============================================================================
# Matrix — OIDC login for Element Web / Synapse
# ============================================================================

resource "authentik_provider_oauth2" "matrix" {
  name               = "matrix-oauth2"
  client_id          = "matrix"
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
      url           = "https://matrix.allegedly.works/_synapse/client/oidc/callback"
    },
  ]
}

resource "authentik_application" "matrix" {
  name              = "Element"
  slug              = "matrix"
  protocol_provider = authentik_provider_oauth2.matrix.id
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

# Secret reflected to matrix namespace; consumed by Matrix HelmRelease via valuesFrom.
resource "kubernetes_secret" "matrix_oidc" {
  metadata {
    name      = "matrix-oidc-config"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "matrix"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "matrix"
    }
  }

  data = {
    # Synapse reads this via HelmRelease valuesFrom (key: values.yaml).
    # The {{ }} templates are Synapse Jinja2 — not HCL interpolation.
    "values.yaml" = <<-EOT
      extraConfig:
        oidc_providers:
          - idp_id: authentik
            idp_name: "Authentik SSO"
            issuer: "https://auth.allegedly.works/application/o/matrix/"
            client_id: "${authentik_provider_oauth2.matrix.client_id}"
            client_secret: "${authentik_provider_oauth2.matrix.client_secret}"
            discover: true
            scopes:
              - openid
              - profile
              - email
            allow_existing_users: true
            enable_registration: true
            user_mapping_provider:
              config:
                localpart_template: "{{ user.preferred_username }}"
                display_name_template: "{{ user.name }}"
                email_template: "{{ user.email }}"
    EOT
  }
}
