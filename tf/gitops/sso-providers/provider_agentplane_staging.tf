# Agentplane staging — OIDC login for the integration app (agentplane-staging.allegedly.works)
#
# The app is the relying party (authlib + a signed session cookie; see x/agentplane/app/oidc.py).
# The application slug is the per-provider issuer path, which the app pins the id token's `iss`
# against, so `agentplane` cannot be renamed without changing the app's configured issuer.

resource "authentik_provider_oauth2" "agentplane_staging" {
  name               = "agentplane-staging-oauth2"
  client_id          = "agentplane-staging"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # The app names an approval after `preferred_username`, which the profile scope carries.
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://agentplane-staging.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "agentplane_staging" {
  name              = "Agentplane staging"
  slug              = "agentplane"
  protocol_provider = authentik_provider_oauth2.agentplane_staging.id
  meta_description  = "Agentplane integration app on staging - sandboxes running Claude Code and Codex on the cheap-experiments key"
  meta_launch_url   = "https://agentplane-staging.allegedly.works"
  open_in_new_tab   = true
}

# Who may log in. Authentik's binding is the whole allowlist: the app treats any identity this
# issuer signs as an operator, so widening access here is what grants it.
resource "authentik_policy_binding" "agentplane_staging_access" {
  target = authentik_application.agentplane_staging.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

# Signs the session cookie (Starlette SessionMiddleware). Generated here so it lives with the
# client credentials and rotates together; a per-pod key would end every session on a restart.
resource "random_password" "agentplane_staging_session_secret" {
  length  = 64
  special = false
}

resource "kubernetes_secret" "agentplane_staging_oidc" {
  metadata {
    name      = "agentplane-oidc"
    namespace = "authentik"
    annotations = {
      description                                                     = "Agentplane staging OAuth client credentials and operator session secret"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "agentplane-staging"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "agentplane-staging"
    }
  }

  data = {
    client-id      = authentik_provider_oauth2.agentplane_staging.client_id
    client-secret  = authentik_provider_oauth2.agentplane_staging.client_secret
    session-secret = random_password.agentplane_staging_session_secret.result
  }
}
