# CLIProxyAPI's built-in management UI/API (cli-proxy-api-admin.allegedly.works) — mints
# and recovers the Codex/Claude OAuth sessions backing OpenClaw's chatgpt/oai-responses/*
# models. cluster/k8s/cli-proxy-api/README.md "Remote recovery" has the recovery flow.
#
# The patched backend owns the OIDC code exchange and browser session. Model API keys
# and the management key used by AIQuota remain independent of browser authentication.
#
# Scoped to the account owner only: this UI mints OAuth credentials against personal
# ChatGPT/Claude accounts, so nobody else who might pass Authentik login should reach it.

resource "authentik_provider_oauth2" "cli_proxy_api_admin" {
  name                  = "cli-proxy-api-admin-oidc"
  client_id             = "cli-proxy-api-admin"
  client_type           = "confidential"
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  signing_key           = data.authentik_certificate_key_pair.self_signed.id
  access_token_validity = "hours=1"

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  # The backend allowlist below uses this same immutable numeric user ID.
  sub_mode = "user_id"
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
  ]
  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://cli-proxy-api-admin.allegedly.works/v0/management/callback"
    },
  ]
}

resource "authentik_application" "cli_proxy_api_admin" {
  name              = "CLIProxyAPI Admin"
  slug              = "cli-proxy-api-admin"
  protocol_provider = authentik_provider_oauth2.cli_proxy_api_admin.id
  meta_description  = "CLIProxyAPI management UI: mint/refresh the Codex and Claude OAuth sessions"
  meta_launch_url   = "https://cli-proxy-api-admin.allegedly.works/management.html"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "cli_proxy_api_admin_owner_only" {
  target = authentik_application.cli_proxy_api_admin.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

resource "kubernetes_secret" "cli_proxy_api_admin_oidc" {
  metadata {
    name      = "cli-proxy-api-admin-oidc"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "cli-proxy-api"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "cli-proxy-api"
    }
  }

  data = {
    MANAGEMENT_OIDC_ISSUER           = "https://auth.allegedly.works/application/o/cli-proxy-api-admin/"
    MANAGEMENT_OIDC_CLIENT_ID        = authentik_provider_oauth2.cli_proxy_api_admin.client_id
    MANAGEMENT_OIDC_CLIENT_SECRET    = authentik_provider_oauth2.cli_proxy_api_admin.client_secret
    MANAGEMENT_OIDC_REDIRECT_URL     = "https://cli-proxy-api-admin.allegedly.works/v0/management/callback"
    MANAGEMENT_OIDC_ALLOWED_SUBJECTS = tostring(authentik_user.agentydragon.id)
  }
}
