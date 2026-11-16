locals {
  confidential_oauth_providers = {
    forgejo = {
      name         = "forgejo-oauth2"
      client_id    = "forgejo"
      redirect_uri = "https://git.allegedly.works/user/oauth2/authentik/callback"
    }
    grafana = {
      name         = "grafana-oauth2"
      client_id    = "grafana"
      redirect_uri = "https://grafana.allegedly.works/login/generic_oauth"
    }
    langfuse = {
      name         = "langfuse-oauth2"
      client_id    = "langfuse"
      redirect_uri = "https://langfuse.allegedly.works/api/auth/callback/custom"
    }
    matrix = {
      name         = "matrix-oauth2"
      client_id    = "matrix"
      redirect_uri = "https://matrix.allegedly.works/_synapse/client/oidc/callback"
    }
    props = {
      name         = "props-oauth2"
      client_id    = "props"
      redirect_uri = "https://props.allegedly.works/auth/callback"
    }
  }
}

resource "authentik_provider_oauth2" "confidential" {
  for_each = local.confidential_oauth_providers

  name               = each.value.name
  client_id          = each.value.client_id
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
      matching_mode     = "strict"
      url               = each.value.redirect_uri
      redirect_uri_type = "authorization"
    },
  ]
}
