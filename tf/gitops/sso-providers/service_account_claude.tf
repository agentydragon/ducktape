# Claude agent — read-only diagnostics service account
#
# Permissions grant visibility into Authentik state (events, apps, users,
# outposts, flows, policies) without exposing credential material.
# The API token is written to a K8s secret in the authentik namespace;
# the claude-rbac RBAC grants Claude's kubectl identity a get on that
# specific secret so the session start hook can load it.

resource "authentik_user" "claude_service_account" {
  username = "claude-service-account"
  name     = "Claude Agent (diagnostics)"
  type     = "service_account"
}

locals {
  claude_diagnostics_permissions = toset([
    # Audit log — biggest gap vs kubectl (events are in DB, not pod logs)
    "authentik_events.view_event",
    # Application and provider inventory
    "authentik_core.view_application",
    "authentik_providers_proxy.view_proxyprovider",
    # Identity
    "authentik_core.view_user",
    "authentik_core.view_group",
    "authentik_core.view_authenticatedsession",
    # Outpost connectivity
    "authentik_outposts.view_outpost",
    "authentik_outposts.view_outpostserviceconnection",
    # Flows and policies
    "authentik_flows.view_flow",
    "authentik_flows.view_flowstagebinding",
    "authentik_policies.view_policy",
    "authentik_policies.view_policybinding",
    # System health
    "authentik_core.view_systemtask",
    "authentik_blueprints.view_blueprintinstance",
  ])
}

resource "authentik_rbac_permission_user" "claude_diagnostics" {
  for_each   = local.claude_diagnostics_permissions
  user       = tonumber(authentik_user.claude_service_account.id)
  permission = each.value
}

resource "authentik_token" "claude_api" {
  identifier   = "claude-diagnostics-api-token"
  user         = authentik_user.claude_service_account.username
  intent       = "api"
  description  = "Read-only diagnostic API token for Claude agent"
  expiring     = false
  retrieve_key = true
}

resource "kubernetes_secret" "claude_authentik_token" {
  metadata {
    name      = "claude-authentik-api-token"
    namespace = "authentik"
    annotations = {
      "description"                                                   = "Authentik API token for Claude agent read-only diagnostics. Reflected into claude-sandbox by Reflector."
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "claude-sandbox"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "claude-sandbox"
    }
  }
  data = {
    token = authentik_token.claude_api.key
    url   = "https://auth.allegedly.works"
  }
}
