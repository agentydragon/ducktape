# ============================================================================
# Langfuse — OIDC login for the LLM observability dashboard
# (langfuse.allegedly.works)
#
# Langfuse is a NextAuth relying party that speaks generic OIDC via its
# AUTH_CUSTOM_* env vars. This provider mints the confidential client; the
# client_id/secret are written to a K8s secret in the authentik namespace and
# reflected into the langfuse namespace, where the HelmRelease consumes them as
# AUTH_CUSTOM_CLIENT_ID / AUTH_CUSTOM_CLIENT_SECRET. Login is gated to the
# admins group; email/password login is disabled in the chart (SSO-only).
# ============================================================================

resource "authentik_provider_oauth2" "langfuse" {
  name               = "langfuse-oauth2"
  client_id          = "langfuse"
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
      url           = "https://langfuse.allegedly.works/api/auth/callback/custom"
    },
  ]
}

resource "authentik_application" "langfuse" {
  name              = "Langfuse"
  slug              = "langfuse"
  protocol_provider = authentik_provider_oauth2.langfuse.id
  meta_description  = "LLM observability and tracing dashboard"
  meta_launch_url   = "https://langfuse.allegedly.works"
  meta_icon         = "https://cdn.simpleicons.org/langfuse"
}

# Gate login to the admins group (which contains agentydragon). Authentik is
# the sole authentication path, so this binding is the access boundary.
resource "authentik_policy_binding" "langfuse_admins" {
  target = authentik_application.langfuse.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

resource "kubernetes_secret" "langfuse_oidc" {
  metadata {
    name      = "langfuse-oidc-config"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "langfuse"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "langfuse"
    }
  }

  data = {
    client-id     = authentik_provider_oauth2.langfuse.client_id
    client-secret = authentik_provider_oauth2.langfuse.client_secret
  }
}
