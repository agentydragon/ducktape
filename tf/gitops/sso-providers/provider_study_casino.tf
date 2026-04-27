# ============================================================================
# Study Casino — OIDC confidential client (Authorization Code flow)
# ============================================================================
#
# The casino handles its own OIDC flow (login/callback/logout endpoints),
# so it no longer needs the Authentik embedded proxy outpost. This resource
# creates the OAuth2 provider and writes the credentials to a k8s Secret in
# the study-casino namespace so the pod can read them at startup.
#
# The Authentik application and policy bindings are managed by the blueprint
# at cluster/k8s/authentik/app/blueprints/study-casino-sso.yaml.

resource "random_password" "study_casino_session_secret" {
  length  = 48
  special = false # only alphanumeric — no quoting issues in env vars
}

resource "authentik_provider_oauth2" "study_casino" {
  name      = "study-casino"
  client_id = "study-casino"
  # confidential — the backend does the code exchange, client_secret stays server-side
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  access_token_validity      = "hours=1"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://casino.allegedly.works/auth/callback"
    }
  ]
}

resource "kubernetes_secret" "study_casino_oidc" {
  metadata {
    name      = "study-casino-oidc"
    namespace = "study-casino"
    annotations = {
      description = "OIDC client credentials and session secret for the Study Casino app"
    }
  }

  data = {
    oidc_client_secret = authentik_provider_oauth2.study_casino.client_secret
    session_secret     = random_password.study_casino_session_secret.result
  }
}
