# Claude agent — read-only diagnostics service account
#
# Permissions grant visibility into Authentik state (events, apps, users,
# outposts, flows, policies) without exposing credential material.
# The API token is written to a K8s secret in the authentik namespace and
# reflected into claude-sandbox by Reflector for the session hook to load.

resource "authentik_user" "claude_service_account" {
  username = "claude-service-account"
  name     = "Claude Agent (diagnostics)"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
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

# authentik_rbac_permission_user has a read-after-create bug in goauthentik/authentik
# ~> 2025.10: POST succeeds but the immediate GET-by-ID returns 404, causing
# "Provider produced inconsistent result after apply" on every run.
# Work around by calling the Authentik API directly from the TF runner pod
# (in-cluster access to authentik-server.svc) via null_resource local-exec.
resource "null_resource" "claude_diagnostics_permissions" {
  triggers = {
    user_id     = authentik_user.claude_service_account.id
    permissions = join(",", sort(tolist(local.claude_diagnostics_permissions)))
  }

  provisioner "local-exec" {
    command = <<-EOT
      echo "$PERMISSIONS" | tr ',' '\n' | while IFS= read -r perm; do
        status=$(curl -s -o /dev/null -w "%%{http_code}" \
          -X POST \
          -H "Authorization: Bearer $AUTHENTIK_TOKEN" \
          -H "Content-Type: application/json" \
          -d "{\"user\": $USER_ID, \"permission\": \"$perm\"}" \
          "http://authentik-server.authentik.svc.cluster.local/api/v3/rbac/permissions/users/")
        echo "permission $perm: HTTP $status"
      done
    EOT
    environment = {
      AUTHENTIK_TOKEN = data.kubernetes_secret.authentik_bootstrap.data["AUTHENTIK_BOOTSTRAP_TOKEN"]
      USER_ID         = tostring(tonumber(authentik_user.claude_service_account.id))
      PERMISSIONS     = join(",", sort(tolist(local.claude_diagnostics_permissions)))
    }
  }
}

resource "authentik_token" "claude_api" {
  identifier   = "claude-diagnostics-api-token"
  user         = authentik_user.claude_service_account.id
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
