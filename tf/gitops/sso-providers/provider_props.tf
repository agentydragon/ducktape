# ============================================================================
# Props — OIDC login for the eval dashboard (props.allegedly.works)
#
# The props backend is the OIDC relying party (authlib + session cookie); see
# props/backend/oidc.py. This provider mints the confidential client; the
# client_id/secret and a generated session-signing secret are written to a K8s
# secret in the authentik namespace and reflected into the props namespace,
# where the Deployment consumes them as PROPS_OIDC_* env vars.
# ============================================================================

resource "authentik_provider_oauth2" "props" {
  name               = "props-oauth2"
  client_id          = "props"
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
      url           = "https://props.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "props" {
  name              = "Props"
  slug              = "props"
  protocol_provider = authentik_provider_oauth2.props.id
  meta_description  = "LLM critic evaluation dashboard"
  meta_launch_url   = "https://props.allegedly.works"
}

# Gate login to the admins group (which contains agentydragon). The backend
# additionally checks the email against PROPS_OIDC_ADMIN_EMAILS (defense in depth).
resource "authentik_policy_binding" "props_admins" {
  target = authentik_application.props.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# Signs the props session cookie (Starlette SessionMiddleware secret_key).
# Generated here so it lives with the client credentials and rotates together.
resource "random_password" "props_session_secret" {
  length  = 64
  special = false
}

resource "kubernetes_secret" "props_oidc" {
  metadata {
    name      = "props-oidc-config"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "props"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "props"
    }
  }

  data = {
    client-id      = authentik_provider_oauth2.props.client_id
    client-secret  = authentik_provider_oauth2.props.client_secret
    session-secret = random_password.props_session_secret.result
  }
}
