# InvenTree — OIDC login for the inventory system (suspended)

resource "authentik_provider_oauth2" "inventree" {
  name               = "inventree-oauth2"
  client_id          = "inventree"
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
      url           = "https://inventree.allegedly.works/accounts/authentik/login/callback/"
    },
  ]
}

resource "authentik_application" "inventree" {
  name              = "InvenTree"
  slug              = "inventree"
  protocol_provider = authentik_provider_oauth2.inventree.id
  meta_description  = "InvenTree Inventory Management"
  meta_publisher    = "InvenTree"
  meta_icon         = "https://raw.githubusercontent.com/inventree/InvenTree/master/assets/images/logo/inventree.svg"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "inventree_admins" {
  target = authentik_application.inventree.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# InvenTree reads INVENTREE_SSO_PROVIDERS env var as a JSON blob.
resource "kubernetes_secret" "inventree_sso_providers" {
  metadata {
    name      = "inventree-sso-providers"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "inventree"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "inventree"
    }
  }

  data = {
    providers = jsonencode({
      openid_connect = {
        APPS = [{
          provider_id = "authentik"
          name        = "Log in via Authentik"
          client_id   = authentik_provider_oauth2.inventree.client_id
          secret      = authentik_provider_oauth2.inventree.client_secret
          settings = {
            server_url = "https://auth.allegedly.works/application/o/inventree/"
          }
        }]
      }
    })
  }
}
