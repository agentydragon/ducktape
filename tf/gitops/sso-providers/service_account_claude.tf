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

# The claude-diagnostics role, its permissions, and the group that carries the
# role are owned by the claude-service-account blueprint at
# cluster/k8s/authentik/app/blueprints/claude-service-account.yaml. The
# blueprint binds the TF-managed user above into the group via
# `!Find [authentik_core.user, [username, claude-service-account]]`.
#
# Why the role/group can't live in TF: both `authentik_rbac_permission_user`
# (broken since ~> 2025.10 — provider Read hits GET /rbac/permissions/users/{id}/
# which Authentik never implemented) and `authentik_rbac_permission_role`
# (broken since e28babb0b8 in July 2024 — the `assign`/`unassign` detail actions
# are unrouted, so every call returns 405) fail at apply time. The blueprint
# reconciles permissions onto the role via Authentik's internal Python APIs,
# bypassing the broken REST endpoint. See
# debug/authentik_rbac_permission_role/README.md for the full diagnosis.

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
