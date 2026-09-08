# CLIProxyAPI's built-in management UI/API (cli-proxy-api-admin.allegedly.works) — mints
# and recovers the Codex/Claude OAuth sessions backing OpenClaw's chatgpt/oai-responses/*
# models. cluster/k8s/cli-proxy-api/README.md "Remote recovery" has the recovery flow.
#
# Pure gatekeeper, not OIDC: CLIProxyAPI has no notion of Authentik identity headers, so
# this proxy provider only blocks unauthenticated access before the app's own
# MANAGEMENT_PASSWORD check runs underneath (cli-proxy-api/deployment.yaml) — the same
# defense-in-depth shape as Airlock's app-level secret. First Terraform-managed proxy
# provider in this repo (existing ones are blueprint-managed pending #987); the embedded
# outpost that actually serves it is still blueprint-managed
# (k8s/authentik/app/blueprints/embedded-outpost.yaml), which is why this provider's name
# also has to be added there — cluster/validation's outpost-assignment check explicitly
# allows a referenced name with no blueprint definition for exactly this split.
#
# Scoped to the account owner only: this UI mints OAuth credentials against personal
# ChatGPT/Claude accounts, so nobody else who might pass Authentik login should reach it.

data "authentik_flow" "cli_proxy_api_admin_authentication" {
  slug = "default-authentication-flow"
}

resource "authentik_provider_proxy" "cli_proxy_api_admin" {
  name                  = "cli-proxy-api-admin"
  mode                  = "proxy"
  external_host         = "https://cli-proxy-api-admin.allegedly.works"
  internal_host         = "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317"
  authentication_flow   = data.authentik_flow.cli_proxy_api_admin_authentication.id
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  access_token_validity = "hours=1"
}

resource "authentik_application" "cli_proxy_api_admin" {
  name              = "CLIProxyAPI Admin"
  slug              = "cli-proxy-api-admin"
  protocol_provider = authentik_provider_proxy.cli_proxy_api_admin.id
  meta_description  = "CLIProxyAPI management UI: mint/refresh the Codex and Claude OAuth sessions"
  meta_launch_url   = "https://cli-proxy-api-admin.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "cli_proxy_api_admin_owner_only" {
  target = authentik_application.cli_proxy_api_admin.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}
