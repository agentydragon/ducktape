# CLIProxyAPI's built-in management UI/API (cli-proxy-api-admin.allegedly.works) — mints
# and recovers the Codex/Claude OAuth sessions backing OpenClaw's chatgpt-sub/oai-responses/*
# models. cluster/k8s/cli-proxy-api/README.md "Remote recovery" has the recovery flow.
#
# The backend owns the OIDC code exchange and browser session. Model API keys
# and the management key used by AIQuota remain independent of browser authentication.
#
# Scoped to the account owner only: this UI mints OAuth credentials against personal
# ChatGPT/Claude accounts, so nobody else who might pass Authentik login should reach it.

resource "authentik_application" "cli_proxy_api_admin" {
  name              = "CLIProxyAPI Admin"
  slug              = "cli-proxy-api-admin"
  protocol_provider = authentik_provider_oauth2.providers["cli_proxy_api_admin"].id
  meta_description  = "CLIProxyAPI management UI: mint/refresh the Codex and Claude OAuth sessions"
  meta_launch_url   = "https://cli-proxy-api-admin.allegedly.works/management.html"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "cli_proxy_api_admin_owner_only" {
  target = authentik_application.cli_proxy_api_admin.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}
