# Paperless-ngx — OIDC login for the document management system.
# Single-user (agentydragon): access is gated to the admins group, and the
# user is auto-provisioned on first login into the `paperless_users` group
# (created by the paperless-bootstrap-group Job) for full non-admin access.
# Not a Django superuser by design — see cluster/k8s/paperless/.

resource "authentik_provider_oauth2" "paperless" {
  name               = "paperless-oauth2"
  client_id          = "paperless"
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

  # django-allauth openid_connect callback (provider_id = "authentik").
  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://paperless.allegedly.works/accounts/oidc/authentik/login/callback/"
    },
  ]
}

resource "authentik_application" "paperless" {
  name              = "Paperless-ngx"
  slug              = "paperless"
  protocol_provider = authentik_provider_oauth2.paperless.id
  meta_description  = "Document management system"
  meta_publisher    = "Paperless-ngx"
  meta_icon         = "https://raw.githubusercontent.com/paperless-ngx/paperless-ngx/main/resources/logo/web/svg/square.svg"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "paperless_admins" {
  target = authentik_application.paperless.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# Paperless reads PAPERLESS_SOCIALACCOUNT_PROVIDERS as a JSON blob.
resource "kubernetes_secret" "paperless_sso_providers" {
  metadata {
    name      = "paperless-sso-providers"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "paperless"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "paperless"
    }
  }

  data = {
    providers = jsonencode({
      openid_connect = {
        OAUTH_PKCE_ENABLED = true
        SCOPE              = ["openid", "profile", "email"]
        APPS = [{
          provider_id = "authentik"
          name        = "Log in via Authentik"
          client_id   = authentik_provider_oauth2.paperless.client_id
          secret      = authentik_provider_oauth2.paperless.client_secret
          settings = {
            server_url     = "https://auth.allegedly.works/application/o/paperless/.well-known/openid-configuration"
            fetch_userinfo = true
          }
        }]
      }
    })
  }
}
