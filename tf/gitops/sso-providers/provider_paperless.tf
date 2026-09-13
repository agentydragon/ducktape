# Paperless-ngx — OIDC login for the document management system.
# Single-user (agentydragon): access and Django superuser status are gated by the
# dedicated `paperless_admins` group. The Paperless bootstrap Job creates the
# matching local groups and their permissions; Authentik owns membership.

resource "authentik_group" "paperless_admins" {
  name  = "paperless_admins"
  users = [tonumber(authentik_user.agentydragon.id)]
}

# Paperless's group synchronizer expects the claim values to be names of existing
# local Django groups. Keep this mapping app-specific rather than exposing every
# Authentik group to Paperless.
resource "authentik_property_mapping_provider_scope" "paperless_groups" {
  name       = "paperless-groups"
  scope_name = "groups"
  expression = <<-EXPR
    authentik_groups = [group.name for group in request.user.ak_groups.all()]
    if "paperless_admins" in authentik_groups:
        return {"groups": ["paperless_admins"]}
    return {"groups": []}
  EXPR
}

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
    authentik_property_mapping_provider_scope.paperless_groups.id,
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
  group  = authentik_group.paperless_admins.id
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
        SCOPE              = ["openid", "profile", "email", "groups"]
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
