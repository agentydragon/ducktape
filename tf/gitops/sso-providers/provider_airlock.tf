# Airlock — server-side OIDC login for the OAuth credential broker.
# The backend exchanges the authorization code and signs an HttpOnly session
# cookie; the browser never receives Authentik access or ID tokens.

resource "random_password" "airlock_session_secret" {
  length  = 64
  special = false
}

resource "authentik_provider_oauth2" "airlock" {
  name               = "airlock-server"
  client_id          = "airlock-server"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  access_token_validity      = "hours=24"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.email.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://airlock.allegedly.works/auth/callback"
    }
  ]
}

resource "authentik_application" "airlock" {
  name              = "Airlock"
  slug              = "airlock-server"
  protocol_provider = authentik_provider_oauth2.airlock.id
  meta_description  = "OAuth credential broker"
  meta_launch_url   = "https://airlock.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "airlock_admins" {
  target = authentik_application.airlock.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

resource "kubernetes_secret" "airlock_oidc" {
  metadata {
    name      = "airlock-oidc-config"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "airlock"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "airlock"
    }
  }

  data = {
    client-id      = authentik_provider_oauth2.airlock.client_id
    client-secret  = authentik_provider_oauth2.airlock.client_secret
    session-secret = random_password.airlock_session_secret.result
  }
}
