# ============================================================================
# kubectl-sandbox-mcp — Scoped kubectl MCP (token exchange → sandbox only)
# ============================================================================
# Authenticates callers via Authentik consent, then exchanges the caller's
# token for one scoped to kubectl-sandbox-users group. Even if the caller is
# a cluster admin, the exchanged token only carries sandbox-level permissions.

## User-facing public OAuth2 client (Claude Code / other MCP clients). No
## client secret — relies on PKCE. Users only need client_id to connect.
resource "authentik_provider_oauth2" "kubectl_sandbox_scoped" {
  name               = "kubectl-sandbox-mcp"
  client_id          = "kubectl-sandbox-mcp"
  client_type        = "public"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # Custom scope mapping overrides the `groups` claim to a fixed
  # `["kubectl-sandbox-users"]` regardless of the authenticating user's actual
  # groups. This achieves privilege scoping at token-issue time — no RFC 8693
  # exchange needed, no confidential secondary client. Even a cluster admin
  # who logs in here only gets sandbox-level RBAC (via the OIDC group claim
  # mapping in kube-apiserver's AuthenticationConfiguration).
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
    authentik_property_mapping_provider_scope.kubectl_sandbox_fixed_groups.id,
  ]

  # - Claude Code: http://localhost:<port>/callback
  # - Claude.ai web (custom connectors): https://claude.ai/api/mcp/auth_callback
  # - kubernetes-mcp-server's built-in callback (browser testing): /oauth/callback
  allowed_redirect_uris = [
    {
      matching_mode = "regex"
      url           = "^http://localhost:[0-9]+/callback$"
    },
    {
      matching_mode = "strict"
      url           = "https://claude.ai/api/mcp/auth_callback"
    },
    {
      matching_mode = "strict"
      url           = "https://kubectl-sandbox-mcp.allegedly.works/oauth/callback"
    },
  ]
}

## Scope mapping: hardcodes `groups = ["kubectl-sandbox-users"]` on the issued
## token. This is what makes kubectl-sandbox-mcp scope-safe: the user's actual
## group memberships are NOT forwarded into the token; kube-apiserver only
## sees the sandbox group.
resource "authentik_property_mapping_provider_scope" "kubectl_sandbox_fixed_groups" {
  name       = "kubectl-sandbox-mcp-fixed-groups"
  scope_name = "groups"
  expression = <<-EXPR
    # Overrides the user's real groups with a fixed sandbox-only group.
    # The kubectl-sandbox-mcp application authenticates users via consent,
    # but the issued token only carries this group — no privilege escalation.
    return {"groups": ["kubectl-sandbox-users"]}
  EXPR
}

## Machine-client scope mapping for kubectl-sandbox-client-credentials.
## This provider is trusted by kube-apiserver, so keep the effective groups as
## an explicit allowlist of known machine principals. Unknown users get no
## Kubernetes RBAC group instead of falling back to sandbox.
resource "authentik_property_mapping_provider_scope" "kubectl_machine_groups" {
  name       = "kubectl-client-credentials-machine-groups"
  scope_name = "groups"
  expression = <<-EXPR
    username = request.user.username
    if username == "ak-kubectl-sandbox-client-credentials-client_credentials":
        return {"groups": ["kubectl-sandbox-users"]}
    if username == "haku-k8s":
        return {"groups": ["haku"]}
    # agent-box VM users map 1:1 to a same-named k8s group (group == username).
    # Add a user by extending this set.
    if username in {"agent-box-codex", "agent-box-zai"}:
        return {"groups": [username]}
    return {"groups": []}
  EXPR
}

resource "authentik_application" "kubectl_sandbox_scoped" {
  name              = "kubectl-sandbox-mcp"
  slug              = "kubectl-sandbox-mcp"
  protocol_provider = authentik_provider_oauth2.kubectl_sandbox_scoped.id
  meta_description  = "Sandbox kubectl MCP — token issued with fixed sandbox group"
  meta_launch_url   = "https://kubectl-sandbox-mcp.allegedly.works"
}

resource "authentik_policy_binding" "kubectl_sandbox_scoped_users" {
  target = authentik_application.kubectl_sandbox_scoped.uuid
  group  = authentik_group.kubectl_sandbox_users.id
  order  = 0
}

## No Kubernetes Secret needed — the OAuth2 provider is public (PKCE) and
## the pod runs in plain passthrough mode (no token exchange). The previous
## kubernetes_secret.kubectl_sandbox_scoped resource will be destroyed on
## the next TF apply.
