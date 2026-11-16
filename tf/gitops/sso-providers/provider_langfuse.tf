# Langfuse — OIDC login for the LLM observability dashboard
# (langfuse.allegedly.works)
#
# Langfuse is a NextAuth relying party that speaks generic OIDC via its
# AUTH_CUSTOM_* env vars. This provider mints the confidential client; the
# client_id/secret are written to a K8s secret in the authentik namespace and
# reflected into the langfuse namespace, where the HelmRelease consumes them as
# AUTH_CUSTOM_CLIENT_ID / AUTH_CUSTOM_CLIENT_SECRET. Login is gated to the
# admins group; email/password login is disabled in the chart (SSO-only).

resource "authentik_application" "langfuse" {
  name              = "Langfuse"
  slug              = "langfuse"
  protocol_provider = authentik_provider_oauth2.confidential["langfuse"].id
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
