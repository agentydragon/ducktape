# ============================================================================
# Study Casino — OIDC confidential client (Authorization Code flow)
# ============================================================================
#
# The casino handles its own OIDC flow (login/callback/logout endpoints),
# so it no longer needs the Authentik embedded proxy outpost. This file owns
# the full Authentik setup: OAuth2 provider, application, and policy bindings,
# plus the k8s Secret the pod reads for credentials.

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

resource "authentik_application" "study_casino" {
  name              = "Study Casino"
  slug              = "study-casino"
  protocol_provider = authentik_provider_oauth2.study_casino.id
  meta_description  = "Habit-tracking casino (personal)"
  meta_launch_url   = "https://casino.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "study_casino_admins" {
  target = authentik_application.study_casino.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

resource "authentik_policy_binding" "study_casino_users" {
  target = authentik_application.study_casino.uuid
  group  = authentik_group.study_casino.id
  order  = 1
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
