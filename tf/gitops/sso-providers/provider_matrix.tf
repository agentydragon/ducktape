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
            client_id: "${authentik_provider_oauth2.confidential["matrix"].client_id}"
            client_secret: "${authentik_provider_oauth2.confidential["matrix"].client_secret}"
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
