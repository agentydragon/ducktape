# ============================================================================
# haku-console operator browser login (retires the forward-auth proxy outpost).
# This provider serves the operator session cookie; the Console MCP OAuth provider and endpoint
# have been retired.

resource "authentik_group" "haku_console_access" {
  name  = "haku-console-access"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_provider_oauth2" "haku_console_operator" {
  name                  = "haku-console"
  client_id             = "haku-console"
  client_type           = "confidential"
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  signing_key           = data.authentik_certificate_key_pair.self_signed.id
  access_token_validity = "hours=1"

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  # `sub` is the stable Authentik user id. Exact issuer + subject resolves to the same canonical
  # Operator UUID; username remains display-only.
  sub_mode = "user_id"

  # offline_access so the operator login yields a refresh token the console persists + self-refreshes
  # for hostexec; the resulting access token is the client assertion the per-host providers federate.
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode     = "strict"
      url               = "https://haku.allegedly.works/auth/callback"
      redirect_uri_type = "authorization"
    },
  ]
}

resource "authentik_application" "haku_console_operator" {
  name              = "Haku"
  slug              = "haku-console"
  protocol_provider = authentik_provider_oauth2.haku_console_operator.id
  meta_description  = "Haku's interactive console (operator browser login)"
  meta_launch_url   = "https://haku.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "haku_console_operator_access" {
  target = authentik_application.haku_console_operator.uuid
  group  = authentik_group.haku_console_access.id
  order  = 0
}

# Signs the operator session cookie (Starlette SessionMiddleware). Generated here so it is
# stable across replicas/restarts (an ephemeral per-pod key would invalidate every other
# replica's sessions). 64 alphanumerics; no special chars (env-var safe).
resource "random_password" "haku_console_operator_session" {
  length  = 64
  special = false
}

# Canonical OAuth client credentials and session key. Reflector mirrors this
# Secret into haku-console after that namespace has been created.
resource "kubernetes_secret" "haku_console_oidc_source" {
  metadata {
    name      = "haku-console-oidc"
    namespace = "authentik"
    annotations = {
      description                                                     = "haku-console operator OAuth credentials and session secret"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "haku-console"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "haku-console"
    }
  }

  data = {
    operator_client_id      = authentik_provider_oauth2.haku_console_operator.client_id
    operator_client_secret  = authentik_provider_oauth2.haku_console_operator.client_secret
    operator_session_secret = random_password.haku_console_operator_session.result
  }
}
